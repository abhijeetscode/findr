from __future__ import annotations

from datetime import datetime

from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk

from findr.domain.entities import Document, SearchHit
from findr.domain.value_objects import SourceType
from findr.ports.embedding_provider import EmbeddingProvider

_SNIPPET_FIELDS = ("body_text", "subject", "sender")

_RESULT_SIZE = 50
# Standard RRF constant (Cormack et al.; also Elasticsearch's default).
_RRF_RANK_CONSTANT = 60
_KNN_NUM_CANDIDATES = 200
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
    docker-compose cluster runs. Both legs go out in one `_msearch`; only
    the BM25 leg carries `highlight`, so a hit found by kNN alone gets
    `_build_snippet`'s plain body-prefix fallback instead of `[...]` markers.
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

    def search(self, user_id: int, query: str) -> list[SearchHit]:
        if not query.strip():
            return []

        user_filter = {"term": {"user_id": user_id}}
        source_filter = {"excludes": ["embedding"]}
        bm25_body = {
            "query": {
                "bool": {
                    "must": {
                        "multi_match": {
                            "query": query,
                            "fields": ["subject^2", "sender", "body_text"],
                        }
                    },
                    "filter": user_filter,
                }
            },
            "highlight": {
                "pre_tags": ["["],
                "post_tags": ["]"],
                "fields": {"body_text": {}, "subject": {}, "sender": {}},
            },
            "_source": source_filter,
            "size": _RESULT_SIZE,
        }
        knn_body = {
            "knn": {
                "field": "embedding",
                "query_vector": self._embedding_provider.embed_query(query),
                "k": _RESULT_SIZE,
                "num_candidates": _KNN_NUM_CANDIDATES,
                "similarity": self._min_similarity,
                "filter": user_filter,
            },
            "_source": source_filter,
            "size": _RESULT_SIZE,
        }
        response = self._client.msearch(
            searches=[
                {"index": self._index},
                bm25_body,
                {"index": self._index},
                knn_body,
            ]
        )
        legs = []
        for leg in response["responses"]:
            if "error" in leg:
                raise RuntimeError(f"Elasticsearch search failed: {leg['error']}")
            legs.append(leg["hits"]["hits"])

        hits: list[SearchHit] = []
        for raw, score in _reciprocal_rank_fusion(legs)[:_RESULT_SIZE]:
            source = raw["_source"]
            document = Document(
                id=int(raw["_id"]),
                user_id=source["user_id"],
                connection_id=source["connection_id"],
                external_id=source["external_id"],
                subject=source.get("subject"),
                sender=source.get("sender"),
                recipients=source.get("recipients"),
                body_text=source.get("body_text"),
                sent_at=_parse_date(source.get("sent_at")),
                thread_id=source.get("thread_id"),
            )
            hits.append(
                SearchHit(
                    document=document,
                    snippet=_build_snippet(raw.get("highlight", {}), source.get("body_text")),
                    score=score,
                    source_type=SourceType(source["source_type"]),
                    external_account=source.get("external_account"),
                )
            )
        return hits

    def index_documents(
        self,
        documents: list[Document],
        source_type: SourceType,
        external_account: str | None,
    ) -> None:
        if not documents:
            return
        actions = [
            {
                "_index": self._index,
                "_id": str(doc.id),
                "_source": self._to_source(doc, source_type, external_account),
            }
            for doc in documents
        ]
        bulk(self._client, actions, refresh=True)

    def _to_source(
        self, doc: Document, source_type: SourceType, external_account: str | None
    ) -> dict:
        source = {
            "user_id": doc.user_id,
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
        # Subject is embedded along with the body: it's often the most
        # meaningful text (an email subject, an uploaded file's name), and
        # it gives subject-only documents a vector at all. A document with
        # no text at all gets no vector — Elasticsearch rejects zero-length
        # vectors under cosine similarity — and stays BM25-only.
        text = "\n\n".join(part for part in (doc.subject, doc.body_text) if part and part.strip())
        if text:
            source["embedding"] = self._embedding_provider.embed_document(text)
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


def _build_snippet(highlight: dict, fallback_body: str | None) -> str:
    for field in _SNIPPET_FIELDS:
        fragments = highlight.get(field)
        if fragments:
            return fragments[0]
    return (fallback_body or "")[:200]


def _parse_date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
