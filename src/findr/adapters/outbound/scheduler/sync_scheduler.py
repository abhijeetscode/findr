from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from elasticsearch import Elasticsearch
from sqlalchemy.orm import sessionmaker

from findr.adapters.outbound.connector_factory import connector_for, oauth_provider_for
from findr.adapters.outbound.crypto.token_cipher import TokenCipher
from findr.adapters.outbound.elasticsearch.search_index_elasticsearch import ElasticsearchIndex
from findr.adapters.outbound.postgres.credential_store_postgres import CredentialStorePostgres
from findr.adapters.outbound.postgres.document_repository_postgres import (
    DocumentRepositoryPostgres,
)
from findr.adapters.outbound.postgres.source_connection_repo_postgres import (
    SourceConnectionRepositoryPostgres,
)
from findr.adapters.outbound.postgres.unit_of_work_postgres import UnitOfWorkPostgres
from findr.adapters.outbound.postgres.uploaded_file_repository_postgres import (
    UploadedFileRepositoryPostgres,
)
from findr.adapters.outbound.system_clock import SystemClock
from findr.application.uploads.requeue_stale_uploads import RequeueStaleUploads
from findr.application.sync.sync_source import SyncSource
from findr.config import Settings
from findr.domain.value_objects import SourceType
from findr.ports.embedding_provider import EmbeddingProvider
from findr.ports.upload_queue import UploadQueue

# How often lost or stuck upload jobs are looked for (specs/upload-chunking.md §7).
STALE_UPLOAD_SWEEP_SECONDS = 5 * 60

logger = logging.getLogger(__name__)


def _run_sync_tick(
    session_factory: sessionmaker,
    settings: Settings,
    es_client: Elasticsearch,
    embedding_provider: EmbeddingProvider,
) -> None:
    db = session_factory()
    try:
        connection_repo = SourceConnectionRepositoryPostgres(db)
        # Uploaded-files connections have nothing external to pull — the
        # upload endpoint already wrote everything (specs/file-upload.md §2.2).
        connections = [
            c for c in connection_repo.list_active() if c.source_type != SourceType.FILE
        ]
        if not connections:
            # No active connections (e.g. no OAuth client id/secret
            # configured yet, or no one has connected anything) — skip
            # constructing the token cipher entirely, so a placeholder
            # FINDR_TOKEN_ENCRYPTION_KEY doesn't crash every idle tick before
            # the user has actually connected anything.
            return

        credential_store = CredentialStorePostgres(db, TokenCipher(settings.token_encryption_key))
        document_repo = DocumentRepositoryPostgres(db)
        search_index = ElasticsearchIndex(
            es_client, settings.elasticsearch_index, embedding_provider
        )
        clock = SystemClock()

        for connection in connections:
            connector = connector_for(connection.source_type, connection.user_id, connection.id)
            oauth_provider = oauth_provider_for(connection.source_type, settings)
            use_case = SyncSource(
                connector,
                oauth_provider,
                credential_store,
                connection_repo,
                document_repo,
                search_index,
                clock,
            )
            try:
                use_case.execute(connection)
                db.commit()
            except Exception:
                # SyncSource itself never raises (it records failure on the
                # connection); this is a last-resort net for anything else,
                # e.g. the commit itself failing, so one bad connection
                # can't abort the rest of the tick's loop.
                logger.exception("Sync tick failed for connection %s", connection.id)
                db.rollback()
    finally:
        db.close()


def _run_stale_upload_sweep(session_factory: sessionmaker, upload_queue: UploadQueue) -> None:
    db = session_factory()
    try:
        requeued = RequeueStaleUploads(
            UploadedFileRepositoryPostgres(db), UnitOfWorkPostgres(db), upload_queue, SystemClock()
        ).execute()
        if requeued:
            logger.info("Re-enqueued stale uploads %s", requeued)
    except Exception:
        logger.exception("Stale-upload sweep failed")
        db.rollback()
    finally:
        db.close()


def create_sync_scheduler(
    session_factory: sessionmaker,
    settings: Settings,
    es_client: Elasticsearch,
    embedding_provider: EmbeddingProvider,
    upload_queue: UploadQueue,
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _run_sync_tick,
        "interval",
        seconds=settings.sync_interval_seconds,
        args=[session_factory, settings, es_client, embedding_provider],
        id="source_sync_tick",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _run_stale_upload_sweep,
        "interval",
        seconds=STALE_UPLOAD_SWEEP_SECONDS,
        args=[session_factory, upload_queue],
        id="stale_upload_sweep",
        max_instances=1,
        coalesce=True,
    )
    return scheduler
