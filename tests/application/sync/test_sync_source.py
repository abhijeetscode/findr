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


class FakeDocumentRepository:
    def __init__(self) -> None:
        self.upserted: list[Document] = []
        self.deleted: list[tuple[int, list[str]]] = []

    def upsert_many(self, documents) -> None:
        self.upserted.extend(documents)

    def delete_many(self, connection_id, external_ids) -> None:
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

    use_case = SyncSource(
        connector, FakeOAuthProvider(), credential_store, connection_repo, document_repo, clock
    )
    use_case.execute(_connection())

    assert document_repo.upserted == [doc]
    assert document_repo.deleted == [(1, ["msg-old"])]
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
        connector, oauth_provider, credential_store, connection_repo, document_repo, clock
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
        connector, FakeOAuthProvider(), credential_store, connection_repo, document_repo, clock
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
        clock,
    )
    use_case.execute(_connection())  # must not raise

    assert connection_repo.status_updates[0][1] == ConnectionStatus.ERROR
    assert "boom" in connection_repo.status_updates[0][2]
