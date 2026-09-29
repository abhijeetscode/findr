from __future__ import annotations

from elasticsearch import Elasticsearch

# See specs/elasticsearch-search.md §4 and specs/gmail-thread-id.md §6.
INDEX_MAPPING = {
    "properties": {
        "user_id": {"type": "keyword"},
        "connection_id": {"type": "keyword"},
        "external_id": {"type": "keyword"},
        "source_type": {"type": "keyword"},
        "external_account": {"type": "keyword"},
        "subject": {"type": "text"},
        "sender": {"type": "text"},
        "recipients": {"type": "text"},
        "body_text": {"type": "text"},
        "sent_at": {"type": "date"},
        "thread_id": {"type": "keyword"},
    }
}


def create_es_client(url: str) -> Elasticsearch:
    return Elasticsearch(url)


def ensure_index(client: Elasticsearch, index_name: str) -> None:
    """Idempotent index creation, called at startup — the Elasticsearch
    equivalent of init_db()'s Base.metadata.create_all()."""
    if not client.indices.exists(index=index_name):
        client.indices.create(index=index_name, mappings=INDEX_MAPPING)
