from cryptography.fernet import Fernet
from sqlalchemy.orm import sessionmaker

from findr.adapters.outbound.scheduler.sync_scheduler import _run_sync_tick
from findr.adapters.outbound.sqlite.db import create_db_engine, init_db
from findr.adapters.outbound.sqlite.source_connection_repo_sqlite import (
    SourceConnectionRepositorySqlite,
)
from findr.adapters.outbound.sqlite.user_repository_sqlite import UserRepositorySqlite
from findr.config import Settings
from findr.domain.value_objects import ConnectionStatus, SourceType


def test_run_sync_tick_runs_end_to_end_for_a_connection_with_no_stored_credentials(monkeypatch):
    # Regression test: _run_sync_tick previously NameError'd on
    # CredentialStoreSqlite (an import dropped during a refactor) before
    # ever reaching a connection. A connection with no stored credentials
    # fails fast *inside* SyncSource (no real network call), so this
    # exercises the real construction path end-to-end without needing a
    # live Gmail/Slack/Notion account.
    monkeypatch.setenv("FINDR_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    engine = create_db_engine(":memory:")
    init_db(engine)
    session_factory = sessionmaker(bind=engine)
    settings = Settings()

    db = session_factory()
    user = UserRepositorySqlite(db).create("a@example.com", "hash")
    db.commit()
    connection = SourceConnectionRepositorySqlite(db).create(
        user.id, SourceType.GMAIL, "a@gmail.com"
    )
    db.commit()
    db.close()

    _run_sync_tick(session_factory, settings)  # must not raise

    db = session_factory()
    updated = SourceConnectionRepositorySqlite(db).get(connection.id, user.id)
    db.close()
    assert updated.status == ConnectionStatus.NEEDS_REAUTH
    assert "No stored credentials" in updated.last_error


def test_run_sync_tick_is_a_noop_with_no_active_connections(monkeypatch):
    engine = create_db_engine(":memory:")
    init_db(engine)
    session_factory = sessionmaker(bind=engine)
    settings = Settings()

    _run_sync_tick(session_factory, settings)  # must not raise, even with no token key set
