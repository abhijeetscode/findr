from __future__ import annotations

import logging
import time
from datetime import datetime

from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk

from findr.domain.entities import Document, DocumentChunk, SearchHit, SearchResults
from findr.domain.value_objects import SourceType
from findr.observability import log_event
from findr.observability.events import elapsed_ms
from findr.ports.embedding_provider import EmbeddingProvider

logger = logging.getLogger(__name__)

_SNIPPET_FIELDS = ("body_text", "subject", "sender")
_SNIPPET_LENGTH = 200

# Only these sources get chunk vectors and semantic search; everything else
# (Gmail today) is keyword-only and never touches the embedding model. Fixed
# in code, deliberately not configurable — see specs/semantic-search.md §12.
EMBEDDED_SOURCE_TYPES = frozenset({SourceType.FILE})

# Vectors never travel back in search responses ("embedding" is the unused
# pre-chunking field on older indices).
_SOURCE_EXCLUDES = ["chunks", "embedding"]

# Standard RRF constant (Cormack et al.; also Elasticsearch's default).
_RRF_RANK_CONSTANT = 60
# Floor and Elasticsearch's ceiling for the kNN shortlist (num_candidates).
_KNN_NUM_CANDIDATES = 200
_KNN_MAX_CANDIDATES = 10_000
# Chunk fields carrying a match's page numbers; and the most matching chunks
# read per document to collect them (Elasticsearch's default
# index.max_inner_result_window). See specs/open-files-and-pdf-pages.md §2.1.
_PAGE_FIELDS = ["chunks.metadata.page_start", "chunks.metadata.page_end"]
_KEYWORD_PAGES_INNER_HITS = "keyword_pages"
_MAX_PAGE_CHUNKS = 100
# kNN always returns its k nearest neighbours, however unrelated — without a
# floor, every query would "match" every document. Cosine similarity of
# L2-normalised Qwen3-Embedding vectors. Tuned against the real model: on a
# probe set, paraphrased-but-relevant pairs scored 0.43-0.58 and unrelated
# pairs (including vague one-word queries like "new") at most 0.37. See
# specs/semantic-search.md §5; overridable via FINDR_SEMANTIC_MIN_SIMILARITY.
DEFAULT_MIN_SIMILARITY = 0.4


class ElasticsearchIndex:
    """Implements ports.search_index.SearchIndex.

    Deviation from the original spec text worth flagging: `index_documents`
    takes `source_type`/`external_account` in addition to the documents.
    `Document` itself doesn't carry either (they're connection-level facts,
    not document-level ones — see domain/entities.py's SearchHit comment for
    why the same split exists there) but Elasticsearch has no JOIN to a
    separate source_connections table the way the old SQLite adapter did, so
    they have to be denormalized onto each indexed document at write time.
    `SyncSource` already has the connection in scope, so it costs nothing to
    pass through.

    Search is hybrid BM25 + kNN, fused with Reciprocal Rank Fusion
    (specs/semantic-search.md §5). The fusion happens here, client-side,
    rather than via Elasticsearch's `rrf` retriever: that retriever is a
    paid-licence feature and returns 403 on the basic licence the
    docker-compose cluster runs. Results are paged in two phases, each one
    `_msearch` with both legs: rank the top N by id, then fetch details for
    one page (specs/search-pagination.md §3). Only the BM25 leg carries
    `highlight`.

    Semantic search covers uploaded files only (EMBEDDED_SOURCE_TYPES,
    specs/semantic-search.md §12), over one vector per chunk, stored nested
    inside the document (specs/upload-chunking.md §6). A document scores by
    its best-matching chunk, and a hit found by kNN alone shows that chunk
    as its snippet.
    """

    def __init__(
        self,
        client: Elasticsearch,
        index_name: str,
        embedding_provider: EmbeddingProvider,
        min_similarity: float = DEFAULT_MIN_SIMILARITY,
    ) -> None:
        self._client = client
        self._index = index_name
        self._embedding_provider = embedding_provider
        self._min_similarity = min_similarity

    def search(
        self,
        user_id: int,
        workspace_id: int,
        query: str,
        *,
        offset: int,
        limit: int,
        max_results: int,
    ) -> SearchResults:
        """Two phases (specs/search-pagination.md §3): rank the top
        `max_results` of both legs by id only and fuse them, then fetch
        highlights, snippets and pages for just this page's documents."""
        if not query.strip():
            return SearchResults(hits=[], total=0, total_is_capped=False)

        # Both the workspace and (as a second guard) the user, on both legs
        # and in both phases: one client's documents never appear in another
        # client's search.
        scope_filter = [
            {"term": {"workspace_id": workspace_id}},
            {"term": {"user_id": user_id}},
        ]
        start = time.perf_counter()
        query_vector = self._embedding_provider.embed_query(query)
        embed_ms = elapsed_ms(start)

        # Phase 1: ranking. Ids only — no _source, highlight or inner hits.
        start = time.perf_counter()
        keyword_ranked, semantic_ranked = self._msearch(
            self._keyword_body(query, scope_filter, size=max_results, details=False),
            self._semantic_body(query_vector, scope_filter, k=max_results, details=False),
        )
        ranking_ms = elapsed_ms(start)
        fused = _reciprocal_rank_fusion([keyword_ranked["hits"]["hits"], semantic_ranked["hits"]["hits"]])
        keyword_total = keyword_ranked["hits"]["total"]
        total_is_capped = (
            keyword_total["value"] > max_results
            or keyword_total.get("relation") == "gte"
            or len(semantic_ranked["hits"]["hits"]) >= max_results
        )
        page = fused[offset : offset + limit]
        page_ids = [raw["_id"] for raw, _ in page]

        # Phase 2: details for this page only (skipped for an empty page).
        start = time.perf_counter()
        hits: list[SearchHit] = []
        if page_ids:
            keyword_details, semantic_details = self._msearch(
                self._keyword_body(
                    query, scope_filter, size=len(page_ids), details=True, ids=page_ids
                ),
                self._semantic_body(
                    query_vector, scope_filter, k=len(page_ids), details=True, ids=page_ids
                ),
            )
            hits = self._to_hits(
                page, keyword_details["hits"]["hits"], semantic_details["hits"]["hits"]
            )
        # Adapter-level detail behind search.executed (specs/logging-telemetry.md §6).
        log_event(
            logger,
            "search_index.searched",
            level=logging.DEBUG,
            embed_ms=embed_ms,
            ranking_ms=ranking_ms,
            details_ms=elapsed_ms(start),
            keyword_hits=len(keyword_ranked["hits"]["hits"]),
            semantic_hits=len(semantic_ranked["hits"]["hits"]),
            fused=len(fused),
        )
        return SearchResults(hits=hits, total=len(fused), total_is_capped=total_is_capped)

    def _msearch(self, keyword_body: dict, semantic_body: dict) -> list[dict]:
        response = self._client.msearch(
            searches=[
                {"index": self._index},
                keyword_body,
                {"index": self._index},
                semantic_body,
            ]
        )
        legs = response["responses"]
        for leg in legs:
            if "error" in leg:
                raise RuntimeError(f"Elasticsearch search failed: {leg['error']}")
        return legs

    def _keyword_body(
        self,
        query: str,
        scope_filter: list[dict],
        *,
        size: int,
        details: bool,
        ids: list[str] | None = None,
    ) -> dict:
        """The BM25 leg. With `details`, it also finds the pages of matching
        chunks and highlights the match; without, it only ranks."""
        bool_query: dict = {
            "must": {
                "multi_match": {
                    "query": query,
                    "fields": ["subject^2", "sender", "body_text"],
                }
            },
            "filter": [*scope_filter, *_ids_filter(ids)],
        }
        if not details:
            return {"query": {"bool": bool_query}, "_source": False, "size": size}
        # Finds which chunks hold the keywords, only to read their pages.
        # Scores 0 and sits in `should` beside a `must`, so it can't change
        # what matches or the ranking. A chunk needs every word: with OR, a
        # common word like "the" would mark nearly every page.
        bool_query["should"] = {
            "constant_score": {
                "filter": {
                    "nested": {
                        "path": "chunks",
                        "query": {
                            "match": {
                                "chunks.text.search": {"query": query, "operator": "and"}
                            }
                        },
                        "inner_hits": {
                            "name": _KEYWORD_PAGES_INNER_HITS,
                            "size": _MAX_PAGE_CHUNKS,
                            "_source": _PAGE_FIELDS,
                        },
                    }
                },
                "boost": 0,
            }
        }
        return {
            "query": {"bool": bool_query},
            "highlight": {
                "pre_tags": ["["],
                "post_tags": ["]"],
                "fields": {"body_text": {}, "subject": {}, "sender": {}},
            },
            "_source": {"excludes": _SOURCE_EXCLUDES},
            "size": size,
        }

    def _semantic_body(
        self,
        query_vector: list[float],
        scope_filter: list[dict],
        *,
        k: int,
        details: bool,
        ids: list[str] | None = None,
    ) -> dict:
        """The kNN leg. With `details`, it also returns the best-matching
        chunk (for the snippet and its pages); without, it only ranks."""
        knn: dict = {
            "field": "chunks.embedding",
            "query_vector": query_vector,
            "k": k,
            # The shortlist must be at least k; twice k keeps the approximate
            # search from missing good neighbours (specs/search-pagination.md §3).
            "num_candidates": min(max(2 * k, _KNN_NUM_CANDIDATES), _KNN_MAX_CANDIDATES),
            "similarity": self._min_similarity,
            # The source_type filter is explicit even though only these
            # sources carry vectors, so stale vectors on anything else can
            # never surface semantically (semantic-search.md §12.4).
            "filter": [
                *scope_filter,
                {"terms": {"source_type": sorted(t.value for t in EMBEDDED_SOURCE_TYPES)}},
                *_ids_filter(ids),
            ],
        }
        if not details:
            return {"knn": knn, "_source": False, "size": k}
        knn["inner_hits"] = {
            "size": 1,
            "_source": ["chunks.text", "chunks.kind", *_PAGE_FIELDS],
        }
        return {"knn": knn, "_source": {"excludes": _SOURCE_EXCLUDES}, "size": k}

    def _to_hits(
        self, page: list[tuple[dict, float]], keyword_raw: list[dict], semantic_raw: list[dict]
    ) -> list[SearchHit]:
        """This page's hits in phase 1's order, with phase 1's RRF scores and
        phase 2's details. A document neither leg returned this time (e.g.
        deleted in between) is dropped."""
        keyword_by_id = {raw["_id"]: raw for raw in keyword_raw}
        semantic_by_id = {raw["_id"]: raw for raw in semantic_raw}
        hits: list[SearchHit] = []
        for ranked, score in page:
            doc_id = ranked["_id"]
            keyword = keyword_by_id.get(doc_id)
            semantic = semantic_by_id.get(doc_id)
            # The BM25 hit when there is one: it carries `highlight`.
            raw = keyword or semantic
            if raw is None:
                continue
            source = raw["_source"]
            document = Document(
                id=int(doc_id),
                user_id=source["user_id"],
                connection_id=source["connection_id"],
                external_id=source["external_id"],
                subject=source.get("subject"),
                sender=source.get("sender"),
                recipients=source.get("recipients"),
                body_text=source.get("body_text"),
                sent_at=_parse_date(source.get("sent_at")),
                thread_id=source.get("thread_id"),
                workspace_id=_parse_int(source.get("workspace_id")),
            )
            keyword_pages = _pages(keyword, _KEYWORD_PAGES_INNER_HITS) if keyword else []
            semantic_pages = _pages(semantic, "chunks") if semantic else []
            hits.append(
                SearchHit(
                    document=document,
                    snippet=_build_snippet(
                        raw.get("highlight", {}),
                        _best_chunk_text(semantic) if semantic else None,
                        source.get("body_text"),
                    ),
                    score=score,
                    source_type=SourceType(source["source_type"]),
                    external_account=source.get("external_account"),
                    pages=keyword_pages or semantic_pages,
                )
            )
        return hits

    def index_documents(
        self,
        documents: list[Document],
        source_type: SourceType,
        external_account: str | None,
        workspace_id: int,
    ) -> None:
        if not documents:
            return
        embed = source_type in EMBEDDED_SOURCE_TYPES
        start = time.perf_counter()
        actions = [
            {
                "_index": self._index,
                "_id": str(doc.id),
                "_source": self._to_source(doc, source_type, external_account, workspace_id, embed),
            }
            for doc in documents
        ]
        embed_ms = elapsed_ms(start)
        start = time.perf_counter()
        bulk(self._client, actions, refresh=True)
        log_event(
            logger,
            "search_index.indexed",
            level=logging.DEBUG,
            source_type=source_type.value,
            documents=len(documents),
            chunks=sum(len(doc.chunks) for doc in documents) if embed else 0,
            embed_ms=embed_ms,
            bulk_ms=elapsed_ms(start),
        )

    def _to_source(
        self,
        doc: Document,
        source_type: SourceType,
        external_account: str | None,
        workspace_id: int,
        embed: bool,
    ) -> dict:
        source = {
            "user_id": doc.user_id,
            "workspace_id": workspace_id,
            "connection_id": doc.connection_id,
            "external_id": doc.external_id,
            "source_type": source_type.value,
            "external_account": external_account,
            "subject": doc.subject,
            "sender": doc.sender,
            "recipients": doc.recipients,
            "body_text": doc.body_text,
            "sent_at": doc.sent_at.isoformat() if doc.sent_at else None,
            "thread_id": doc.thread_id,
        }
        # A document without chunks (e.g. an upload from before chunking)
        # stays keyword-only.
        if embed and doc.chunks:
            vectors = self._embedding_provider.embed_documents([c.text for c in doc.chunks])
            source["chunks"] = [
                _chunk_to_source(chunk, vector)
                for chunk, vector in zip(doc.chunks, vectors, strict=True)
            ]
        return source

    def delete_documents(self, connection_id: int, external_ids: list[str]) -> None:
        if not external_ids:
            return
        self._client.delete_by_query(
            index=self._index,
            query={
                "bool": {
                    "filter": [
                        {"term": {"connection_id": connection_id}},
                        {"terms": {"external_id": external_ids}},
                    ]
                }
            },
            conflicts="proceed",
            refresh=True,
        )

    def delete_workspace(self, workspace_id: int) -> None:
        self._client.delete_by_query(
            index=self._index,
            query={"term": {"workspace_id": workspace_id}},
            conflicts="proceed",
            refresh=True,
        )


def _ids_filter(ids: list[str] | None) -> list[dict]:
    return [{"ids": {"values": ids}}] if ids is not None else []


def _reciprocal_rank_fusion(legs: list[list[dict]]) -> list[tuple[dict, float]]:
    """Combines ranked hit lists by rank position alone — BM25 and cosine
    scores live on incompatible scales, which is the point of RRF. A hit
    appearing in several legs keeps the first leg's raw hit (the BM25 one,
    which carries `highlight`)."""
    scores: dict[str, float] = {}
    first_seen: dict[str, dict] = {}
    for leg in legs:
        for rank, raw in enumerate(leg, start=1):
            doc_id = raw["_id"]
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (_RRF_RANK_CONSTANT + rank)
            first_seen.setdefault(doc_id, raw)
    ranked = sorted(scores, key=lambda doc_id: scores[doc_id], reverse=True)
    return [(first_seen[doc_id], scores[doc_id]) for doc_id in ranked]


def _chunk_to_source(chunk: DocumentChunk, vector: list[float]) -> dict:
    metadata = chunk.metadata
    return {
        "text": chunk.text,
        "kind": chunk.kind.value,
        "table_html": chunk.table_html,
        "embedding": vector,
        "metadata": {
            "chunk_index": metadata.chunk_index,
            "document_version": metadata.document_version,
            "content_sha256": metadata.content_sha256,
            "workspace_id": metadata.workspace_id,
            "filename": metadata.filename,
            "mime_type": metadata.mime_type,
            "page_start": metadata.page_start,
            "page_end": metadata.page_end,
            "section_title": metadata.section_title,
            "element_types": metadata.element_types,
            "languages": metadata.languages,
            "is_continuation": metadata.is_continuation,
            "parser_version": metadata.parser_version,
        },
    }


def _best_chunk_text(raw: dict) -> str | None:
    inner = raw.get("inner_hits", {}).get("chunks", {}).get("hits", {}).get("hits", [])
    return inner[0]["_source"].get("text") if inner else None


def _pages(raw: dict, inner_hits_name: str) -> list[int]:
    """Every page the named inner hits' chunks span, sorted and deduplicated.
    A chunk without page numbers (non-PDF) contributes none."""
    inner = raw.get("inner_hits", {}).get(inner_hits_name, {}).get("hits", {}).get("hits", [])
    pages: set[int] = set()
    for chunk in inner:
        metadata = chunk.get("_source", {}).get("metadata", {})
        start, end = metadata.get("page_start"), metadata.get("page_end")
        if start is not None:
            pages.update(range(start, (end if end is not None else start) + 1))
    return sorted(pages)


def _build_snippet(highlight: dict, matched_chunk: str | None, fallback_body: str | None) -> str:
    """Keyword highlight if BM25 found the document; otherwise the chunk
    kNN matched; otherwise the start of the body."""
    for field in _SNIPPET_FIELDS:
        fragments = highlight.get(field)
        if fragments:
            return fragments[0]
    if matched_chunk:
        return matched_chunk[:_SNIPPET_LENGTH]
    return (fallback_body or "")[:_SNIPPET_LENGTH]


def _parse_int(value) -> int | None:
    return int(value) if value is not None else None


def _parse_date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
