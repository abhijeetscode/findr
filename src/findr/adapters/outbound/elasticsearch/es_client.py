from __future__ import annotations

from elasticsearch import Elasticsearch

# Qwen3-Embedding-0.6B's full output dimension. Every document ever indexed
# must share it — changing it means a new index and a full reindex, not a
# mapping update. See specs/semantic-search.md §3.
EMBEDDING_DIMS = 1024

# Nested chunks of an uploaded document: one vector per chunk, plus the
# chunk's text (for snippets, not keyword search — that stays on body_text)
# and its metadata, explicitly typed so future filters such as "latest
# document_version only" are exact matches. See
# specs/upload-chunking.md §6.4.
CHUNKS_FIELD_MAPPING = {
    "type": "nested",
    "properties": {
        "text": {"type": "text", "index": False},
        "kind": {"type": "keyword"},
        "table_html": {"type": "text", "index": False},
        "embedding": {
            "type": "dense_vector",
            "dims": EMBEDDING_DIMS,
            "index": True,
            "similarity": "cosine",
        },
        "metadata": {
            "properties": {
                "chunk_index": {"type": "integer"},
                "document_version": {"type": "integer"},
                "content_sha256": {"type": "keyword"},
                "filename": {"type": "keyword"},
                "mime_type": {"type": "keyword"},
                "page_start": {"type": "integer"},
                "page_end": {"type": "integer"},
                "section_title": {"type": "text", "index": False},
                "element_types": {"type": "keyword"},
                "languages": {"type": "keyword"},
                "is_continuation": {"type": "boolean"},
                "parser_version": {"type": "keyword"},
            }
        },
    },
}

# See specs/elasticsearch-search.md §4, specs/gmail-thread-id.md §6 and
# specs/upload-chunking.md §6.4. (Indices created before chunking also
# carry an unused top-level "embedding" field — mappings can't drop fields.)
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
        "chunks": CHUNKS_FIELD_MAPPING,
    }
}


def create_es_client(url: str) -> Elasticsearch:
    return Elasticsearch(url)


def ensure_index(client: Elasticsearch, index_name: str) -> None:
    """Idempotent index creation, called at startup — the Elasticsearch
    equivalent of init_db()'s Base.metadata.create_all(). For an index that
    predates chunking, additively puts the nested chunks field onto its
    existing mapping (a no-op if it's already there); existing documents
    only gain chunk vectors once scripts/reindex_search.py is re-run."""
    if not client.indices.exists(index=index_name):
        client.indices.create(index=index_name, mappings=INDEX_MAPPING)
        return
    client.indices.put_mapping(index=index_name, properties={"chunks": CHUNKS_FIELD_MAPPING})
