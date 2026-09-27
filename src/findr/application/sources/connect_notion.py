from __future__ import annotations

from findr.domain.entities import SourceConnection
from findr.domain.exceptions import InvalidOAuthState
from findr.domain.value_objects import ConnectionStatus, SourceType
from findr.ports.credential_store import CredentialStore
from findr.ports.oauth_provider import OAuthProvider
from findr.ports.oauth_state_repository import OAuthStateRepository
from findr.ports.source_connection_repo import SourceConnectionRepository

OAUTH_STATE_TTL_SECONDS = 600


class BeginNotionConnect:
    def __init__(self, oauth_provider: OAuthProvider, oauth_states: OAuthStateRepository) -> None:
        self._oauth_provider = oauth_provider
        self._oauth_states = oauth_states

    def execute(self, user_id: int) -> str:
        """Returns the Notion authorize URL the caller should redirect to.

        No PKCE (Notion's OAuth doesn't support it) — the state row is still
        created for CSRF protection, with an unused code_verifier. See
        specs/notion-connector.md section 6.
        """
        state = self._oauth_states.create(user_id, "", OAUTH_STATE_TTL_SECONDS)
        return self._oauth_provider.build_authorize_url(state, "")


class CompleteNotionConnect:
    def __init__(
        self,
        oauth_provider: OAuthProvider,
        oauth_states: OAuthStateRepository,
        connection_repo: SourceConnectionRepository,
        credential_store: CredentialStore,
    ) -> None:
        self._oauth_provider = oauth_provider
        self._oauth_states = oauth_states
        self._connection_repo = connection_repo
        self._credential_store = credential_store

    def execute(self, code: str, state: str) -> SourceConnection:
        # Same fixation-resistant binding as Gmail/Slack.
        oauth_state = self._oauth_states.consume(state)
        if oauth_state is None:
            raise InvalidOAuthState("Unknown or expired OAuth state")

        credentials = self._oauth_provider.exchange_code(code, oauth_state.code_verifier)
        # Must be called on the same provider instance right after
        # exchange_code() — see NotionOAuthProvider's docstring for why.
        workspace_id = self._oauth_provider.get_account_email(credentials.access_token)
        display_name = self._oauth_provider.get_display_name(credentials.access_token)

        connection = self._connection_repo.get_by_account(
            oauth_state.user_id, SourceType.NOTION, workspace_id
        )
        if connection is None:
            connection = self._connection_repo.create(
                oauth_state.user_id, SourceType.NOTION, workspace_id, display_name
            )
        else:
            self._connection_repo.update_status(connection.id, ConnectionStatus.ACTIVE)
            self._connection_repo.update_display_name(connection.id, display_name)

        self._credential_store.save(
            connection.id,
            credentials.access_token,
            credentials.refresh_token,
            credentials.expires_at,
        )
        return connection
