from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import sessionmaker

from findr.adapters.outbound.crypto.token_cipher import TokenCipher
from findr.adapters.outbound.gmail.gmail_connector import GmailConnector
from findr.adapters.outbound.gmail.gmail_oauth_provider import GmailOAuthProvider
from findr.adapters.outbound.notion.notion_connector import NotionConnector
from findr.adapters.outbound.notion.notion_oauth_provider import NotionOAuthProvider
from findr.adapters.outbound.slack.slack_connector import SlackConnector
from findr.adapters.outbound.slack.slack_oauth_provider import SlackOAuthProvider
from findr.adapters.outbound.sqlite.credential_store_sqlite import CredentialStoreSqlite
from findr.adapters.outbound.sqlite.document_repository_sqlite import DocumentRepositorySqlite
from findr.adapters.outbound.sqlite.source_connection_repo_sqlite import (
    SourceConnectionRepositorySqlite,
)
from findr.adapters.outbound.system_clock import SystemClock
from findr.application.sync.sync_source import SyncSource
from findr.config import Settings
from findr.domain.value_objects import SourceType
from findr.ports.oauth_provider import OAuthProvider
from findr.ports.source_connector import SourceConnector

logger = logging.getLogger(__name__)


def _oauth_provider_for(source_type: SourceType, settings: Settings) -> OAuthProvider:
    if source_type == SourceType.GMAIL:
        return GmailOAuthProvider(
            client_id=settings.google_oauth_client_id,
            client_secret=settings.google_oauth_client_secret,
            redirect_uri=settings.google_oauth_redirect_uri,
        )
    if source_type == SourceType.SLACK:
        return SlackOAuthProvider(
            client_id=settings.slack_client_id,
            client_secret=settings.slack_client_secret,
            redirect_uri=settings.slack_redirect_uri,
        )
    if source_type == SourceType.NOTION:
        return NotionOAuthProvider(
            client_id=settings.notion_client_id,
            client_secret=settings.notion_client_secret,
            redirect_uri=settings.notion_redirect_uri,
        )
    raise ValueError(f"No OAuthProvider configured for source type {source_type!r}")


def _connector_for(source_type: SourceType, user_id: int, connection_id: int) -> SourceConnector:
    if source_type == SourceType.GMAIL:
        return GmailConnector(user_id=user_id, connection_id=connection_id)
    if source_type == SourceType.SLACK:
        return SlackConnector(user_id=user_id, connection_id=connection_id)
    if source_type == SourceType.NOTION:
        return NotionConnector(user_id=user_id, connection_id=connection_id)
    raise ValueError(f"No SourceConnector configured for source type {source_type!r}")


def _run_sync_tick(session_factory: sessionmaker, settings: Settings) -> None:
    db = session_factory()
    try:
        connection_repo = SourceConnectionRepositorySqlite(db)
        connections = connection_repo.list_active()
        if not connections:
            # No active connections (e.g. no OAuth client id/secret
            # configured yet, or no one has connected anything) — skip
            # constructing the token cipher entirely, so a placeholder
            # FINDR_TOKEN_ENCRYPTION_KEY doesn't crash every idle tick before
            # the user has actually connected anything.
            return

        credential_store = CredentialStoreSqlite(db, TokenCipher(settings.token_encryption_key))
        document_repo = DocumentRepositorySqlite(db)
        clock = SystemClock()

        for connection in connections:
            connector = _connector_for(connection.source_type, connection.user_id, connection.id)
            oauth_provider = _oauth_provider_for(connection.source_type, settings)
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
        id="source_sync_tick",
        max_instances=1,
        coalesce=True,
    )
    return scheduler
