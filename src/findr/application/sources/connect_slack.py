from __future__ import annotations

from findr.domain.entities import SourceConnection
from findr.domain.exceptions import InvalidOAuthState
from findr.domain.value_objects import ConnectionStatus, SourceType
from findr.ports.credential_store import CredentialStore
from findr.ports.oauth_provider import OAuthProvider
from findr.ports.oauth_state_repository import OAuthStateRepository
from findr.ports.source_connection_repo import SourceConnectionRepository

# Same TTL as Gmail's connect flow — how long the user has to complete
# Slack's consent screen before the state pair expires.
OAUTH_STATE_TTL_SECONDS = 600


class BeginSlackConnect:
    def __init__(self, oauth_provider: OAuthProvider, oauth_states: OAuthStateRepository) -> None:
        self._oauth_provider = oauth_provider
        self._oauth_states = oauth_states

    def execute(self, user_id: int) -> str:
        """Returns the Slack authorize URL the caller should redirect to.

        No PKCE (Slack's OAuth v2 doesn't support it) — the state row is
        still created for CSRF protection, with an unused code_verifier.
        See specs/slack-connector.md section 6.
        """
        state = self._oauth_states.create(user_id, "", OAUTH_STATE_TTL_SECONDS)
        return self._oauth_provider.build_authorize_url(state, "")


class CompleteSlackConnect:
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
        # Same fixation-resistant binding as Gmail: the connection belongs
        # to whoever initiated the connect, not whatever session is current
        # when Slack redirects back. See specs/gmail-connector.md section 5.
        oauth_state = self._oauth_states.consume(state)
        if oauth_state is None:
            raise InvalidOAuthState("Unknown or expired OAuth state")

        credentials = self._oauth_provider.exchange_code(code, oauth_state.code_verifier)
        dedup_key = self._oauth_provider.get_account_email(credentials.access_token)
        display_name = self._oauth_provider.get_display_name(credentials.access_token)

        connection = self._connection_repo.get_by_account(
            oauth_state.user_id, SourceType.SLACK, dedup_key
        )
        if connection is None:
            connection = self._connection_repo.create(
                oauth_state.user_id, SourceType.SLACK, dedup_key, display_name
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
