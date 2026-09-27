from datetime import datetime

import pytest

from findr.application.sources.connect_slack import BeginSlackConnect, CompleteSlackConnect
from findr.domain.entities import Credentials, SourceConnection
from findr.domain.exceptions import InvalidOAuthState
from findr.domain.value_objects import ConnectionStatus
from findr.ports.oauth_state_repository import OAuthState


class FakeOAuthProvider:
    """Stand-in for ports.oauth_provider.OAuthProvider — no real network."""

    def __init__(self) -> None:
        self.built_urls: list[tuple[str, str]] = []

    def build_authorize_url(self, state: str, code_challenge: str) -> str:
        self.built_urls.append((state, code_challenge))
        return f"https://slack.com/oauth/v2/authorize?state={state}"

    def exchange_code(self, code: str, code_verifier: str) -> Credentials:
        return Credentials(
            access_token=f"access-for-{code}",
            refresh_token=f"refresh-for-{code}",
            expires_at=datetime(2030, 1, 1),
        )

    def refresh(self, refresh_token: str) -> Credentials:
        raise NotImplementedError

    def revoke(self, credentials: Credentials) -> None:
        pass

    def get_account_email(self, access_token: str) -> str:
        return "T12345:U67890"

    def get_display_name(self, access_token: str) -> str:
        return "Acme Corp (Ada Lovelace)"


class FakeOAuthStateRepository:
    def __init__(self) -> None:
        self._states: dict[str, OAuthState] = {}
        self._counter = 0

    def create(self, user_id: int, code_verifier: str, ttl_seconds: int) -> str:
        self._counter += 1
        state = f"state-{self._counter}"
        self._states[state] = OAuthState(
            state=state,
            user_id=user_id,
            code_verifier=code_verifier,
            expires_at=datetime(2030, 1, 1),
        )
        return state

    def consume(self, state: str) -> OAuthState | None:
        return self._states.pop(state, None)


class FakeSourceConnectionRepository:
    def __init__(self) -> None:
        self._connections: dict[int, SourceConnection] = {}
        self._next_id = 1

    def create(self, user_id, source_type, external_account, display_name=None) -> SourceConnection:
        connection = SourceConnection(
            id=self._next_id,
            user_id=user_id,
            source_type=source_type,
            external_account=external_account,
            status=ConnectionStatus.ACTIVE,
            sync_cursor=None,
            last_synced_at=None,
            last_error=None,
            created_at=datetime(2024, 1, 1),
            display_name=display_name,
        )
        self._connections[connection.id] = connection
        self._next_id += 1
        return connection

    def get(self, connection_id, user_id) -> SourceConnection | None:
        c = self._connections.get(connection_id)
        return c if c is not None and c.user_id == user_id else None

    def get_by_account(self, user_id, source_type, external_account) -> SourceConnection | None:
        for c in self._connections.values():
            if (
                c.user_id == user_id
                and c.source_type == source_type
                and c.external_account == external_account
            ):
                return c
        return None

    def list_for_user(self, user_id) -> list[SourceConnection]:
        return [c for c in self._connections.values() if c.user_id == user_id]

    def list_active(self) -> list[SourceConnection]:
        return [c for c in self._connections.values() if c.status == ConnectionStatus.ACTIVE]

    def update_status(self, connection_id, status, last_error=None) -> None:
        c = self._connections[connection_id]
        self._connections[connection_id] = SourceConnection(
            id=c.id,
            user_id=c.user_id,
            source_type=c.source_type,
            external_account=c.external_account,
            status=status,
            sync_cursor=c.sync_cursor,
            last_synced_at=c.last_synced_at,
            last_error=last_error,
            created_at=c.created_at,
            display_name=c.display_name,
        )

    def update_cursor(self, connection_id, cursor, synced_at) -> None:
        c = self._connections[connection_id]
        self._connections[connection_id] = SourceConnection(
            id=c.id,
            user_id=c.user_id,
            source_type=c.source_type,
            external_account=c.external_account,
            status=c.status,
            sync_cursor=cursor,
            last_synced_at=synced_at,
            last_error=c.last_error,
            created_at=c.created_at,
            display_name=c.display_name,
        )

    def update_display_name(self, connection_id, display_name) -> None:
        c = self._connections[connection_id]
        self._connections[connection_id] = SourceConnection(
            id=c.id,
            user_id=c.user_id,
            source_type=c.source_type,
            external_account=c.external_account,
            status=c.status,
            sync_cursor=c.sync_cursor,
            last_synced_at=c.last_synced_at,
            last_error=c.last_error,
            created_at=c.created_at,
            display_name=display_name,
        )


class FakeCredentialStore:
    def __init__(self) -> None:
        self._creds: dict[int, Credentials] = {}

    def save(self, connection_id, access_token, refresh_token, expires_at) -> None:
        self._creds[connection_id] = Credentials(access_token, refresh_token, expires_at)

    def get(self, connection_id) -> Credentials | None:
        return self._creds.get(connection_id)

    def delete(self, connection_id) -> None:
        self._creds.pop(connection_id, None)


def test_begin_slack_connect_returns_authorize_url_without_pkce():
    oauth_provider = FakeOAuthProvider()
    oauth_states = FakeOAuthStateRepository()
    use_case = BeginSlackConnect(oauth_provider, oauth_states)

    url = use_case.execute(user_id=1)

    assert url.startswith("https://slack.com/oauth/v2/authorize?state=")
    assert len(oauth_provider.built_urls) == 1
    state, code_challenge = oauth_provider.built_urls[0]
    assert code_challenge == ""  # no PKCE for Slack


def test_complete_slack_connect_creates_connection_keyed_by_team_and_user():
    oauth_provider = FakeOAuthProvider()
    oauth_states = FakeOAuthStateRepository()
    connection_repo = FakeSourceConnectionRepository()
    credential_store = FakeCredentialStore()

    authorize_url = BeginSlackConnect(oauth_provider, oauth_states).execute(user_id=42)
    state = authorize_url.rsplit("state=", 1)[1]

    complete = CompleteSlackConnect(oauth_provider, oauth_states, connection_repo, credential_store)
    connection = complete.execute(code="valid-code", state=state)

    assert connection.user_id == 42
    assert connection.external_account == "T12345:U67890"
    assert connection.display_name == "Acme Corp (Ada Lovelace)"
    assert connection.status == ConnectionStatus.ACTIVE
    assert credential_store.get(connection.id).access_token == "access-for-valid-code"


def test_complete_slack_connect_rejects_unknown_state():
    use_case = CompleteSlackConnect(
        FakeOAuthProvider(),
        FakeOAuthStateRepository(),
        FakeSourceConnectionRepository(),
        FakeCredentialStore(),
    )

    with pytest.raises(InvalidOAuthState):
        use_case.execute(code="valid-code", state="never-issued")


def test_complete_slack_connect_reuses_existing_connection_on_reconnect():
    oauth_provider = FakeOAuthProvider()
    oauth_states = FakeOAuthStateRepository()
    connection_repo = FakeSourceConnectionRepository()
    credential_store = FakeCredentialStore()

    def do_connect():
        state = oauth_states.create(user_id=7, code_verifier="", ttl_seconds=600)
        complete = CompleteSlackConnect(
            oauth_provider, oauth_states, connection_repo, credential_store
        )
        return complete.execute(code="valid-code", state=state)

    first = do_connect()
    second = do_connect()

    assert first.id == second.id
    assert len(connection_repo.list_for_user(7)) == 1
