from __future__ import annotations

from datetime import datetime

from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk

from findr.domain.entities import Document, SearchHit
from findr.domain.value_objects import SourceType

_SNIPPET_FIELDS = ("body_text", "subject", "sender")


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
    """

    def __init__(self, client: Elasticsearch, index_name: str) -> None:
        self._client = client
        self._index = index_name

    def search(self, user_id: int, query: str) -> list[SearchHit]:
        if not query.strip():
            return []

        response = self._client.search(
            index=self._index,
            query={
                "bool": {
                    "must": {
                        "multi_match": {
                            "query": query,
                            "fields": ["subject^2", "sender", "body_text"],
                        }
                    },
                    "filter": {"term": {"user_id": user_id}},
                }
            },
            highlight={
                "pre_tags": ["["],
                "post_tags": ["]"],
                "fields": {"body_text": {}, "subject": {}, "sender": {}},
            },
            size=50,
        )

        hits: list[SearchHit] = []
        for raw in response["hits"]["hits"]:
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
                    score=raw["_score"] or 0.0,
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
                "_source": {
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
                },
            }
            for doc in documents
        ]
        bulk(self._client, actions, refresh=True)

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


def _build_snippet(highlight: dict, fallback_body: str | None) -> str:
    for field in _SNIPPET_FIELDS:
        fragments = highlight.get(field)
        if fragments:
            return fragments[0]
    return (fallback_body or "")[:200]


def _parse_date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
