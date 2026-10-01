from cryptography.fernet import Fernet

from findr.adapters.outbound.postgres.source_connection_repo_postgres import (
    SourceConnectionRepositoryPostgres,
)
from findr.adapters.outbound.postgres.user_repository_postgres import UserRepositoryPostgres
from findr.adapters.outbound.scheduler.sync_scheduler import _run_sync_tick
from findr.config import Settings
from findr.domain.value_objects import ConnectionStatus, SourceType
from conftest import ensure_workspace


def test_run_sync_tick_runs_end_to_end_for_a_connection_with_no_stored_credentials(
    monkeypatch, db_session_factory, es_client, es_index, fake_embedding_provider
):
    # Regression test: _run_sync_tick previously NameError'd on
    # CredentialStorePostgres (an import dropped during a refactor) before
    # ever reaching a connection. A connection with no stored credentials
    # fails fast *inside* SyncSource (no real network call), so this
    # exercises the real construction path end-to-end without needing a
    # live Gmail account.
    monkeypatch.setenv("FINDR_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    # elasticsearch_index has a validation_alias, so Settings(elasticsearch_index=...)
    # is silently ignored by pydantic-settings — only the env var (or the
    # alias name as a kwarg) actually overrides it.
    monkeypatch.setenv("FINDR_ELASTICSEARCH_INDEX", es_index)
    settings = Settings()

    db = db_session_factory()
    user = UserRepositoryPostgres(db).create("a@example.com", "hash")
    db.commit()
    connection = SourceConnectionRepositoryPostgres(db).create(
        user.id, ensure_workspace(db, user.id).id, SourceType.GMAIL, "a@gmail.com"
    )
    db.commit()
    db.close()

    _run_sync_tick(db_session_factory, settings, es_client, fake_embedding_provider)  # must not raise

    db = db_session_factory()
    updated = SourceConnectionRepositoryPostgres(db).get(connection.id, user.id)
    db.close()
    assert updated.status == ConnectionStatus.NEEDS_REAUTH
    assert "No stored credentials" in updated.last_error


def test_run_sync_tick_is_a_noop_with_no_active_connections(
    monkeypatch, db_session_factory, es_client, es_index, fake_embedding_provider
):
    monkeypatch.setenv("FINDR_ELASTICSEARCH_INDEX", es_index)
    settings = Settings()

    _run_sync_tick(db_session_factory, settings, es_client, fake_embedding_provider)  # must not raise, even with no token key set


def test_run_sync_tick_skips_uploaded_files_connections(
    monkeypatch, db_session_factory, es_client, es_index, fake_embedding_provider
):
    # connector_for/oauth_provider_for have no FILE branch — reaching them
    # would raise. There's nothing to sync for uploads (specs/file-upload.md §2.2).
    monkeypatch.setenv("FINDR_ELASTICSEARCH_INDEX", es_index)
    settings = Settings()

    db = db_session_factory()
    user = UserRepositoryPostgres(db).create("a@example.com", "hash")
    connection = SourceConnectionRepositoryPostgres(db).create(
        user.id, ensure_workspace(db, user.id).id, SourceType.FILE, None, display_name="Uploaded files"
    )
    db.commit()
    db.close()

    _run_sync_tick(db_session_factory, settings, es_client, fake_embedding_provider)

    db = db_session_factory()
    unchanged = SourceConnectionRepositoryPostgres(db).get(connection.id, user.id)
    db.close()
    assert unchanged.status == ConnectionStatus.ACTIVE
    assert unchanged.last_error is None
    assert unchanged.last_synced_at is None


def test_stale_upload_sweep_requeues_lost_uploads(db_session_factory, upload_queue):
    from datetime import timedelta

    from findr.adapters.outbound.postgres.uploaded_file_repository_postgres import (
        UploadedFileRepositoryPostgres,
    )
    from findr.adapters.outbound.scheduler.sync_scheduler import _run_stale_upload_sweep
    from findr.adapters.outbound.system_clock import SystemClock

    class PastClock:
        def now(self):
            return SystemClock().now() - timedelta(hours=1)

    db = db_session_factory()
    user = UserRepositoryPostgres(db).create("a@example.com", "hash")
    # Created an hour ago and never picked up — its message was lost.
    lost = UploadedFileRepositoryPostgres(db, clock=PastClock()).create(
        user_id=user.id,
        workspace_id=ensure_workspace(db, user.id).id,
        original_filename="lost.txt",
        mime_type="text/plain",
        file_size_bytes=4,
        storage_path=f"{user.id}/lost.txt",
        content_sha256="a" * 64,
        document_version=1,
    )
    db.commit()
    db.close()

    _run_stale_upload_sweep(db_session_factory, upload_queue)

    assert upload_queue.enqueued == [lost.id]
