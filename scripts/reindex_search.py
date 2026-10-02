"""On-demand backfill: Postgres -> Elasticsearch. See
specs/elasticsearch-search.md §3.4. Bootstraps a fresh environment or
recovers from Elasticsearch data loss, and rebuilds uploaded documents'
chunk vectors from the document_chunks table (specs/semantic-search.md §4,
specs/upload-chunking.md §6.4). Run manually:

    uv run python scripts/reindex_search.py

Not a scheduled job — an operator-triggered action. Prints nothing: the
result is the `reindex.completed` event in <FINDR_LOG_DIR>/reindex.log.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from findr.adapters.outbound.elasticsearch.es_client import (  # noqa: E402
    create_es_client,
    ensure_index,
)
from findr.adapters.outbound.elasticsearch.search_index_elasticsearch import (  # noqa: E402
    ElasticsearchIndex,
)
from findr.adapters.outbound.embeddings.sentence_transformer_provider import (  # noqa: E402
    create_embedding_provider,
)
from findr.adapters.outbound.postgres.db import create_db_engine  # noqa: E402
from findr.adapters.outbound.postgres.document_repository_postgres import (  # noqa: E402
    load_chunks,
)
from findr.adapters.outbound.postgres.models import DocumentModel, SourceConnectionModel  # noqa: E402
from findr.domain.entities import Document  # noqa: E402
from findr.domain.value_objects import SourceType  # noqa: E402
from findr.config import Settings  # noqa: E402
from findr.observability import bind, log_event, new_id  # noqa: E402
from findr.observability.setup import configure_logging  # noqa: E402
from findr.observability.events import elapsed_ms  # noqa: E402
from findr.ports.embedding_provider import EmbeddingProvider  # noqa: E402

BATCH_SIZE = 500

logger = logging.getLogger("findr.scripts.reindex_search")


def _to_document(row: DocumentModel) -> Document:
    return Document(
        id=row.id,
        user_id=row.user_id,
        connection_id=row.connection_id,
        external_id=row.external_id,
        subject=row.subject,
        sender=row.sender,
        recipients=row.recipients,
        body_text=row.body_text,
        sent_at=row.sent_at,
        thread_id=row.thread_id,
        workspace_id=row.workspace_id,
    )


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Module-level so tests can swap in a fake instead of the real model."""
    return create_embedding_provider(settings)


def main() -> None:
    settings = Settings()
    # Logs go to <FINDR_LOG_DIR>/reindex.log, not the console
    # (specs/logging-telemetry.md §4.6).
    configure_logging(settings, "script", log_name="reindex")
    with bind(request_id=new_id("reindex")):
        _reindex(settings)


def _reindex(settings: Settings) -> None:
    start = time.perf_counter()
    engine = create_db_engine(settings.database_url)
    session_factory = sessionmaker(bind=engine)

    es_client = create_es_client(settings.elasticsearch_url)
    ensure_index(es_client, settings.elasticsearch_index)
    search_index = ElasticsearchIndex(
        es_client, settings.elasticsearch_index, build_embedding_provider(settings)
    )

    db = session_factory()
    try:
        # One connection's source_type/external_account at a time, since
        # index_documents needs both (see ports/search_index.py) and a
        # single Postgres query batch can span multiple connections.
        connections = db.execute(select(SourceConnectionModel)).scalars().all()
        total = 0
        total_chunks = 0
        for connection in connections:
            offset = 0
            while True:
                rows = (
                    db.execute(
                        select(DocumentModel)
                        .where(DocumentModel.connection_id == connection.id)
                        .order_by(DocumentModel.id)
                        .offset(offset)
                        .limit(BATCH_SIZE)
                    )
                    .scalars()
                    .all()
                )
                if not rows:
                    break
                documents = [_to_document(row) for row in rows]
                # Chunk vectors are rebuilt from document_chunks — no file is
                # re-parsed (specs/upload-chunking.md §6.4).
                chunks = load_chunks(db, [doc.id for doc in documents])
                for doc in documents:
                    doc.chunks = chunks.get(doc.id, [])
                    total_chunks += len(doc.chunks)
                search_index.index_documents(
                    documents,
                    SourceType(connection.source_type),
                    connection.external_account,
                    connection.workspace_id,
                )
                total += len(documents)
                offset += BATCH_SIZE
        log_event(
            logger,
            "reindex.completed",
            f"Reindexed {total} document(s) from Postgres into Elasticsearch",
            documents=total,
            chunks=total_chunks,
            duration_ms=elapsed_ms(start),
        )
    finally:
        db.close()
        es_client.close()


if __name__ == "__main__":
    main()
