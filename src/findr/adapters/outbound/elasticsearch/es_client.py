from __future__ import annotations

from elasticsearch import Elasticsearch

# Qwen3-Embedding-0.6B's full output dimension. Every document ever indexed
# must share it — changing it means a new index and a full reindex, not a
# mapping update. See specs/semantic-search.md §3.
EMBEDDING_DIMS = 1024

EMBEDDING_FIELD_MAPPING = {
    "type": "dense_vector",
    "dims": EMBEDDING_DIMS,
    "index": True,
    "similarity": "cosine",
}

# See specs/elasticsearch-search.md §4, specs/gmail-thread-id.md §6 and
# specs/semantic-search.md §1.
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
        "embedding": EMBEDDING_FIELD_MAPPING,
    }
}


def create_es_client(url: str) -> Elasticsearch:
    return Elasticsearch(url)


def ensure_index(client: Elasticsearch, index_name: str) -> None:
    """Idempotent index creation, called at startup — the Elasticsearch
    equivalent of init_db()'s Base.metadata.create_all(). For an index that
    predates semantic search, additively puts the embedding field onto its
    existing mapping (a no-op if it's already there); existing documents
    only gain vectors once scripts/reindex_search.py is re-run."""
    if not client.indices.exists(index=index_name):
        client.indices.create(index=index_name, mappings=INDEX_MAPPING)
        return
    client.indices.put_mapping(
        index=index_name, properties={"embedding": EMBEDDING_FIELD_MAPPING}
    )
