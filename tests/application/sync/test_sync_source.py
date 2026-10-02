from datetime import datetime

from findr.application.sync.sync_source import SyncSource
from findr.domain.entities import Credentials, Document, SourceConnection
from findr.domain.exceptions import SourceAuthError, SourceCursorExpired
from findr.domain.value_objects import ConnectionStatus, SourceType
from findr.ports.source_connector import ChangeBatch


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


class FakeCredentialStore:
    def __init__(self, initial: dict[int, Credentials] | None = None) -> None:
        self._creds = dict(initial or {})

    def save(self, connection_id, access_token, refresh_token, expires_at) -> None:
        self._creds[connection_id] = Credentials(access_token, refresh_token, expires_at)

    def get(self, connection_id) -> Credentials | None:
        return self._creds.get(connection_id)

    def delete(self, connection_id) -> None:
        self._creds.pop(connection_id, None)


class FakeOAuthProvider:
    def __init__(self, refresh_result: Credentials | None = None, refresh_error=None) -> None:
        self._refresh_result = refresh_result
        self._refresh_error = refresh_error
        self.refresh_calls: list[str] = []

    def build_authorize_url(self, state, code_challenge):
        raise NotImplementedError

    def exchange_code(self, code, code_verifier):
        raise NotImplementedError

    def refresh(self, refresh_token: str) -> Credentials:
        self.refresh_calls.append(refresh_token)
        if self._refresh_error is not None:
            raise self._refresh_error
        return self._refresh_result

    def revoke(self, token):
        pass

    def get_account_email(self, access_token):
        raise NotImplementedError

    def get_display_name(self, access_token):
        raise NotImplementedError


class FakeSourceConnectionRepository:
    def __init__(self) -> None:
        self.status_updates: list[tuple[int, ConnectionStatus, str | None]] = []
        self.cursor_updates: list[tuple[int, str, datetime]] = []

    def create(self, *a, **k):
        raise NotImplementedError

    def get(self, *a, **k):
        raise NotImplementedError

    def get_by_account(self, *a, **k):
        raise NotImplementedError

    def list_for_user(self, *a, **k):
        raise NotImplementedError

    def list_active(self):
        raise NotImplementedError

    def update_status(self, connection_id, status, last_error=None) -> None:
        self.status_updates.append((connection_id, status, last_error))

    def update_cursor(self, connection_id, cursor, synced_at) -> None:
        self.cursor_updates.append((connection_id, cursor, synced_at))

    def update_display_name(self, *a, **k):
        raise NotImplementedError


class FakeDocumentRepository:
    def __init__(self) -> None:
        self.upserted: list[Document] = []
        self.deleted: list[tuple[int, list[str]]] = []
        self._next_id = 1

    def upsert_many(self, documents) -> list[Document]:
        persisted = []
        for doc in documents:
            doc.id = self._next_id
            self._next_id += 1
            persisted.append(doc)
        self.upserted.extend(persisted)
        return persisted

    def delete_many(self, connection_id, external_ids) -> None:
        self.deleted.append((connection_id, external_ids))


class FakeSearchIndex:
    def __init__(self) -> None:
        self.indexed: list[tuple[list[Document], SourceType, str | None, int]] = []
        self.deleted: list[tuple[int, list[str]]] = []

    def search(self, user_id, query):
        raise NotImplementedError

    def index_documents(self, documents, source_type, external_account, workspace_id) -> None:
        self.indexed.append((documents, source_type, external_account, workspace_id))

    def delete_documents(self, connection_id, external_ids) -> None:
        self.deleted.append((connection_id, external_ids))


class FakeConnector:
    def __init__(self, responses: list[ChangeBatch] | None = None, error=None) -> None:
        self._responses = list(responses or [])
        self._error = error
        self.calls: list[tuple[Credentials, str | None]] = []

    def fetch_changes(self, credentials, cursor):
        self.calls.append((credentials, cursor))
        if self._error is not None:
            raise self._error
        return self._responses.pop(0)


def _connection(**overrides) -> SourceConnection:
    base = dict(
        id=1,
        user_id=10,
        workspace_id=3,
        source_type=SourceType.GMAIL,
        external_account="a@gmail.com",
        status=ConnectionStatus.ACTIVE,
        sync_cursor="old-cursor",
        last_synced_at=None,
        last_error=None,
        created_at=datetime(2024, 1, 1),
    )
    base.update(overrides)
    return SourceConnection(**base)


def test_sync_source_upserts_and_deletes_then_marks_active():
    clock = FixedClock(datetime(2024, 6, 1))
    credentials = Credentials("access", "refresh", expires_at=datetime(2024, 6, 2))
    credential_store = FakeCredentialStore({1: credentials})
    connection_repo = FakeSourceConnectionRepository()
    document_repo = FakeDocumentRepository()
    doc = Document(
        id=0,
        user_id=10,
        connection_id=1,
        external_id="msg-1",
        subject="s",
        sender="s@e.com",
        recipients=None,
        body_text="b",
        sent_at=None,
    )
    batch = ChangeBatch(upserts=[doc], deleted_external_ids=["msg-old"], new_cursor="new-cursor")
    connector = FakeConnector(responses=[batch])
    search_index = FakeSearchIndex()

    use_case = SyncSource(
        connector,
        FakeOAuthProvider(),
        credential_store,
        connection_repo,
        document_repo,
        search_index,
        clock,
    )
    use_case.execute(_connection())

    assert document_repo.upserted == [doc]
    assert document_repo.deleted == [(1, ["msg-old"])]
    # Indexed under the connection's workspace (specs/workspaces.md §5.4).
    assert search_index.indexed == [([doc], SourceType.GMAIL, "a@gmail.com", 3)]
    assert search_index.deleted == [(1, ["msg-old"])]
    assert connection_repo.cursor_updates == [(1, "new-cursor", clock.current)]
    assert connection_repo.status_updates == [(1, ConnectionStatus.ACTIVE, None)]
    assert connector.calls == [(credentials, "old-cursor")]


def test_sync_source_refreshes_expired_credentials_before_syncing():
    clock = FixedClock(datetime(2024, 6, 1))
    expired = Credentials("old-access", "refresh", expires_at=datetime(2024, 5, 1))
    refreshed = Credentials("new-access", "refresh", expires_at=datetime(2024, 6, 2))
    credential_store = FakeCredentialStore({1: expired})
    oauth_provider = FakeOAuthProvider(refresh_result=refreshed)
    connection_repo = FakeSourceConnectionRepository()
    document_repo = FakeDocumentRepository()
    batch = ChangeBatch(upserts=[], deleted_external_ids=[], new_cursor="cursor")
    connector = FakeConnector(responses=[batch])

    use_case = SyncSource(
        connector,
        oauth_provider,
        credential_store,
        connection_repo,
        document_repo,
        FakeSearchIndex(),
        clock,
    )
    use_case.execute(_connection())

    assert oauth_provider.refresh_calls == ["refresh"]
    assert credential_store.get(1) == refreshed
    assert connector.calls == [(refreshed, "old-cursor")]
    assert connection_repo.status_updates == [(1, ConnectionStatus.ACTIVE, None)]


def test_sync_source_marks_needs_reauth_when_no_stored_credentials():
    clock = FixedClock(datetime(2024, 6, 1))
    connection_repo = FakeSourceConnectionRepository()

    use_case = SyncSource(
        FakeConnector(),
        FakeOAuthProvider(),
        FakeCredentialStore(),
        connection_repo,
        FakeDocumentRepository(),
        FakeSearchIndex(),
        clock,
    )
    use_case.execute(_connection())  # must not raise

    assert connection_repo.status_updates[0][0:2] == (1, ConnectionStatus.NEEDS_REAUTH)


def test_sync_source_marks_needs_reauth_when_refresh_fails():
    clock = FixedClock(datetime(2024, 6, 1))
    expired = Credentials("old-access", "bad-refresh", expires_at=datetime(2024, 5, 1))
    credential_store = FakeCredentialStore({1: expired})
    oauth_provider = FakeOAuthProvider(refresh_error=SourceAuthError("invalid_grant"))
    connection_repo = FakeSourceConnectionRepository()

    use_case = SyncSource(
        FakeConnector(),
        oauth_provider,
        credential_store,
        connection_repo,
        FakeDocumentRepository(),
        FakeSearchIndex(),
        clock,
    )
    use_case.execute(_connection())

    assert connection_repo.status_updates[0][1] == ConnectionStatus.NEEDS_REAUTH


def test_sync_source_falls_back_to_full_resync_when_cursor_expired():
    clock = FixedClock(datetime(2024, 6, 1))
    credentials = Credentials("access", "refresh", expires_at=datetime(2024, 6, 2))
    credential_store = FakeCredentialStore({1: credentials})
    connection_repo = FakeSourceConnectionRepository()
    document_repo = FakeDocumentRepository()
    full_batch = ChangeBatch(upserts=[], deleted_external_ids=[], new_cursor="brand-new-cursor")

    class ExpiringThenFullConnector:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        def fetch_changes(self, credentials, cursor):
            self.calls.append(cursor)
            if cursor is not None:
                raise SourceCursorExpired("too old")
            return full_batch

    connector = ExpiringThenFullConnector()
    use_case = SyncSource(
        connector,
        FakeOAuthProvider(),
        credential_store,
        connection_repo,
        document_repo,
        FakeSearchIndex(),
        clock,
    )
    use_case.execute(_connection(sync_cursor="ancient-cursor"))

    assert connector.calls == ["ancient-cursor", None]
    assert connection_repo.cursor_updates == [(1, "brand-new-cursor", clock.current)]
    assert connection_repo.status_updates == [(1, ConnectionStatus.ACTIVE, None)]


def test_sync_source_marks_error_on_unexpected_failure_without_raising():
    clock = FixedClock(datetime(2024, 6, 1))
    credentials = Credentials("access", "refresh", expires_at=datetime(2024, 6, 2))
    credential_store = FakeCredentialStore({1: credentials})
    connection_repo = FakeSourceConnectionRepository()
    connector = FakeConnector(error=RuntimeError("boom"))

    use_case = SyncSource(
        connector,
        FakeOAuthProvider(),
        credential_store,
        connection_repo,
        FakeDocumentRepository(),
        FakeSearchIndex(),
        clock,
    )
    use_case.execute(_connection())  # must not raise

    assert connection_repo.status_updates[0][1] == ConnectionStatus.ERROR
    assert "boom" in connection_repo.status_updates[0][2]


def _sync_with(connector, oauth_provider=None, credentials=None):
    clock = FixedClock(datetime(2024, 6, 1))
    creds = credentials or Credentials("access", "refresh", expires_at=datetime(2024, 6, 2))
    use_case = SyncSource(
        connector,
        oauth_provider or FakeOAuthProvider(),
        FakeCredentialStore({1: creds}),
        FakeSourceConnectionRepository(),
        FakeDocumentRepository(),
        FakeSearchIndex(),
        clock,
    )
    return use_case.execute(_connection())


def _sync_events(caplog):
    return [r for r in caplog.records if getattr(r, "event", None) == "sync.connection"]


def test_sync_logs_counts_and_returns_the_status(caplog):
    # specs/logging-telemetry.md §6.
    caplog.set_level("DEBUG")
    batch = ChangeBatch(upserts=[], deleted_external_ids=["a", "b"], new_cursor="c")

    assert _sync_with(FakeConnector(responses=[batch])) == ConnectionStatus.ACTIVE

    [record] = _sync_events(caplog)
    assert (record.outcome, record.upserts, record.deletes) == ("ok", 0, 2)
    assert record.levelname == "INFO"


def test_sync_failure_is_logged_with_its_traceback(caplog):
    # Gap §7.1: the error used to be stored on the connection and never logged.
    caplog.set_level("DEBUG")

    assert _sync_with(FakeConnector(error=RuntimeError("boom"))) == ConnectionStatus.ERROR

    [record] = _sync_events(caplog)
    assert (record.outcome, record.levelname) == ("error", "WARNING")
    assert record.exc_info[0] is RuntimeError


def test_sync_needing_reauth_is_logged_without_a_traceback(caplog):
    caplog.set_level("DEBUG")
    expired = Credentials("old", "refresh", expires_at=datetime(2024, 5, 1))
    oauth = FakeOAuthProvider(refresh_error=SourceAuthError("invalid_grant"))

    status = _sync_with(FakeConnector(), oauth_provider=oauth, credentials=expired)

    assert status == ConnectionStatus.NEEDS_REAUTH
    [record] = _sync_events(caplog)
    assert (record.outcome, record.levelname) == ("needs_reauth", "WARNING")
    assert not record.exc_info


def test_sync_logs_never_carry_mail_content_or_addresses(tmp_path):
    # specs/logging-telemetry.md §8: not on success, and not when an
    # upstream error message quotes an address.
    import json
    import logging

    from findr.config import Settings
    from findr.observability.setup import configure_logging

    path = configure_logging(
        Settings(FINDR_LOG_DIR=str(tmp_path), FINDR_LOG_LEVEL="DEBUG"), "api"
    )
    doc = Document(
        id=0,
        user_id=10,
        connection_id=1,
        external_id="msg-1",
        subject="Project Zanzibar term sheet",
        sender="ceo@globex.example",
        recipients="cfo@globex.example",
        body_text="Confidential pricing",
        sent_at=None,
    )
    ok = ChangeBatch(upserts=[doc], deleted_external_ids=[], new_cursor="c")
    _sync_with(FakeConnector(responses=[ok]))
    _sync_with(FakeConnector(error=RuntimeError("Delegation denied for ceo@globex.example")))

    for handler in logging.getLogger().handlers:
        handler.flush()
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["outcome"] for r in records if r.get("event") == "sync.connection"] == [
        "ok",
        "error",
    ]
    text = path.read_text().lower()
    for secret in ("zanzibar", "globex", "confidential"):
        assert secret not in text
    assert "[email]" in text
