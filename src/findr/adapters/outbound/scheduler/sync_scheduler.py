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
from findr.adapters.outbound.system_clock import SystemClock
from findr.application.sync.sync_source import SyncSource
from findr.config import Settings

logger = logging.getLogger(__name__)


def _run_sync_tick(session_factory: sessionmaker, settings: Settings, es_client: Elasticsearch) -> None:
    db = session_factory()
    try:
        connection_repo = SourceConnectionRepositoryPostgres(db)
        connections = connection_repo.list_active()
        if not connections:
            # No active connections (e.g. no OAuth client id/secret
            # configured yet, or no one has connected anything) — skip
            # constructing the token cipher entirely, so a placeholder
            # FINDR_TOKEN_ENCRYPTION_KEY doesn't crash every idle tick before
            # the user has actually connected anything.
            return

        credential_store = CredentialStorePostgres(db, TokenCipher(settings.token_encryption_key))
        document_repo = DocumentRepositoryPostgres(db)
        search_index = ElasticsearchIndex(es_client, settings.elasticsearch_index)
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


def create_sync_scheduler(
    session_factory: sessionmaker, settings: Settings, es_client: Elasticsearch
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _run_sync_tick,
        "interval",
        seconds=settings.sync_interval_seconds,
        args=[session_factory, settings, es_client],
        id="source_sync_tick",
        max_instances=1,
        coalesce=True,
    )
    return scheduler
