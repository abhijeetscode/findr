from dataclasses import replace
from datetime import datetime

import pytest

from findr.application.sources.connect_gmail import BeginGmailConnect, CompleteGmailConnect
from findr.domain.entities import Credentials, SourceConnection, Workspace
from findr.domain.exceptions import InvalidOAuthState, SourceInOtherWorkspace
from findr.domain.value_objects import ConnectionStatus, SourceType
from findr.ports.oauth_state_repository import OAuthState


class FakeOAuthProvider:
    """Stand-in for ports.oauth_provider.OAuthProvider — no real network."""

    def __init__(self, email: str = "someone@gmail.com") -> None:
        self.built_urls: list[tuple[str, str]] = []
        self._email = email

    def build_authorize_url(self, state: str, code_challenge: str) -> str:
        self.built_urls.append((state, code_challenge))
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    def exchange_code(self, code: str, code_verifier: str) -> Credentials:
        return Credentials(
            access_token=f"access-for-{code}",
            refresh_token=f"refresh-for-{code}",
            expires_at=datetime(2030, 1, 1),
        )

    def refresh(self, refresh_token: str) -> Credentials:
        raise NotImplementedError

    def revoke(self, token) -> None:
        pass

    def get_account_email(self, access_token: str) -> str:
        return self._email

    def get_display_name(self, access_token: str) -> str:
        return self._email


class FakeOAuthStateRepository:
    def __init__(self) -> None:
        self._states: dict[str, OAuthState] = {}
        self._counter = 0

    def create(self, user_id: int, workspace_id: int, code_verifier: str, ttl_seconds: int) -> str:
        self._counter += 1
        state = f"state-{self._counter}"
        self._states[state] = OAuthState(
            state=state,
            user_id=user_id,
            workspace_id=workspace_id,
            code_verifier=code_verifier,
            expires_at=datetime(2030, 1, 1),
        )
        return state

    def consume(self, state: str) -> OAuthState | None:
        return self._states.pop(state, None)


class FakeSourceConnectionRepository:
    def __init__(self) -> None:
        self.connections: dict[int, SourceConnection] = {}
        self._next_id = 1

    def create(self, user_id, workspace_id, source_type, external_account, display_name=None):
        connection = SourceConnection(
            id=self._next_id,
            user_id=user_id,
            workspace_id=workspace_id,
            source_type=source_type,
            external_account=external_account,
            status=ConnectionStatus.ACTIVE,
            sync_cursor=None,
            last_synced_at=None,
            last_error=None,
            created_at=datetime(2024, 1, 1),
            display_name=display_name,
        )
        self.connections[connection.id] = connection
        self._next_id += 1
        return connection

    def get_by_account(self, user_id, source_type, external_account):
        return next(
            (
                c
                for c in self.connections.values()
                if (c.user_id, c.source_type, c.external_account)
                == (user_id, source_type, external_account)
            ),
            None,
        )

    def update_status(self, connection_id, status, last_error=None) -> None:
        self.connections[connection_id] = replace(
            self.connections[connection_id], status=status, last_error=last_error
        )

    def update_display_name(self, connection_id, display_name) -> None:
        self.connections[connection_id] = replace(
            self.connections[connection_id], display_name=display_name
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


class FakeWorkspaceRepository:
    def __init__(self, *workspaces: Workspace) -> None:
        self._workspaces = {w.id: w for w in workspaces}

    def get(self, workspace_id, user_id):
        w = self._workspaces.get(workspace_id)
        return w if w is not None and w.user_id == user_id else None


def _workspace(id_, name, user_id=7):
    return Workspace(id=id_, user_id=user_id, name=name, created_at=datetime(2026, 1, 1))


class Env:
    def __init__(self) -> None:
        self.oauth_provider = FakeOAuthProvider()
        self.oauth_states = FakeOAuthStateRepository()
        self.connections = FakeSourceConnectionRepository()
        self.credentials = FakeCredentialStore()
        self.workspaces = FakeWorkspaceRepository(_workspace(1, "Client A"), _workspace(2, "Client B"))

    def connect(self, user_id=7, workspace_id=1) -> SourceConnection:
        url = BeginGmailConnect(self.oauth_provider, self.oauth_states).execute(user_id, workspace_id)
        state = url.rsplit("state=", 1)[1]
        return CompleteGmailConnect(
            self.oauth_provider, self.oauth_states, self.connections, self.credentials, self.workspaces
        ).execute(code="valid-code", state=state)


def test_begin_gmail_connect_returns_authorize_url_and_remembers_the_workspace():
    env = Env()
    url = BeginGmailConnect(env.oauth_provider, env.oauth_states).execute(7, 2)

    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?state=")
    state = env.oauth_states.consume(url.rsplit("state=", 1)[1])
    assert (state.user_id, state.workspace_id) == (7, 2)


def test_complete_gmail_connect_creates_connection_in_the_states_workspace():
    env = Env()

    connection = env.connect(user_id=7, workspace_id=2)

    assert connection.user_id == 7
    assert connection.workspace_id == 2
    assert connection.source_type == SourceType.GMAIL
    assert connection.external_account == "someone@gmail.com"
    assert connection.status == ConnectionStatus.ACTIVE
    assert env.credentials.get(connection.id).access_token == "access-for-valid-code"


def test_complete_gmail_connect_rejects_unknown_state():
    env = Env()
    use_case = CompleteGmailConnect(
        env.oauth_provider, env.oauth_states, env.connections, env.credentials, env.workspaces
    )

    with pytest.raises(InvalidOAuthState):
        use_case.execute(code="valid-code", state="never-issued")


def test_reconnect_in_the_same_workspace_reuses_the_connection():
    env = Env()
    first = env.connect(workspace_id=1)
    env.connections.update_status(first.id, ConnectionStatus.NEEDS_REAUTH)

    second = env.connect(workspace_id=1)

    assert second.id == first.id
    assert len(env.connections.connections) == 1
    assert env.connections.connections[first.id].status == ConnectionStatus.ACTIVE


@pytest.mark.parametrize("status", [ConnectionStatus.ACTIVE, ConnectionStatus.DISCONNECTED])
def test_account_already_in_another_workspace_is_refused(status):
    # One Gmail account, one workspace — even a disconnected connection still
    # has its documents there (specs/workspaces.md §5.2).
    env = Env()
    first = env.connect(workspace_id=1)
    env.connections.update_status(first.id, status)

    with pytest.raises(SourceInOtherWorkspace, match="'Client A'"):
        env.connect(workspace_id=2)

    assert len(env.connections.connections) == 1
    assert env.connections.connections[first.id].workspace_id == 1
