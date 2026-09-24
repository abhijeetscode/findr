from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import sessionmaker

from findr.adapters.outbound.crypto.token_cipher import TokenCipher
from findr.adapters.outbound.gmail.gmail_connector import GmailConnector
from findr.adapters.outbound.gmail.gmail_oauth_provider import GmailOAuthProvider
from findr.adapters.outbound.sqlite.credential_store_sqlite import CredentialStoreSqlite
from findr.adapters.outbound.sqlite.document_repository_sqlite import DocumentRepositorySqlite
from findr.adapters.outbound.sqlite.source_connection_repo_sqlite import (
    SourceConnectionRepositorySqlite,
)
from findr.adapters.outbound.system_clock import SystemClock
from findr.application.sync.sync_source import SyncSource
from findr.config import Settings
from findr.domain.value_objects import SourceType

logger = logging.getLogger(__name__)


def _run_sync_tick(session_factory: sessionmaker, settings: Settings) -> None:
    db = session_factory()
    try:
        connection_repo = SourceConnectionRepositorySqlite(db)
        connections = connection_repo.list_active()
        if not connections:
            # No active connections (e.g. Gmail client id/secret not
            # configured yet, or no one has connected anything) — skip
            # constructing the oauth provider / token cipher entirely, so a
            # placeholder FINDR_TOKEN_ENCRYPTION_KEY doesn't crash every
            # idle tick before the user has actually connected anything.
            return

        oauth_provider = GmailOAuthProvider(
            client_id=settings.google_oauth_client_id,
            client_secret=settings.google_oauth_client_secret,
            redirect_uri=settings.google_oauth_redirect_uri,
        )
        credential_store = CredentialStoreSqlite(db, TokenCipher(settings.token_encryption_key))
        document_repo = DocumentRepositorySqlite(db)
        clock = SystemClock()

        for connection in connections:
            if connection.source_type != SourceType.GMAIL:
                continue  # only Gmail is implemented; future connectors add a branch here
            connector = GmailConnector(user_id=connection.user_id, connection_id=connection.id)
            use_case = SyncSource(
                connector, oauth_provider, credential_store, connection_repo, document_repo, clock
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


def create_sync_scheduler(session_factory: sessionmaker, settings: Settings) -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _run_sync_tick,
        "interval",
        seconds=settings.sync_interval_seconds,
        args=[session_factory, settings],
        id="gmail_sync_tick",
        max_instances=1,
        coalesce=True,
    )
    return scheduler
