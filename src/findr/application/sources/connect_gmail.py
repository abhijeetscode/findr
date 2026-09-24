from __future__ import annotations

import base64
import hashlib
import secrets

from findr.domain.entities import SourceConnection
from findr.domain.exceptions import InvalidOAuthState
from findr.domain.value_objects import ConnectionStatus, SourceType
from findr.ports.credential_store import CredentialStore
from findr.ports.oauth_provider import OAuthProvider
from findr.ports.oauth_state_repository import OAuthStateRepository
from findr.ports.source_connection_repo import SourceConnectionRepository

# How long the user has to complete Google's consent screen before the
# state/PKCE pair expires and the callback is rejected.
OAUTH_STATE_TTL_SECONDS = 600


def _generate_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class BeginGmailConnect:
    def __init__(self, oauth_provider: OAuthProvider, oauth_states: OAuthStateRepository) -> None:
        self._oauth_provider = oauth_provider
        self._oauth_states = oauth_states

    def execute(self, user_id: int) -> str:
        """Returns the Google authorize URL the caller should redirect to."""
        verifier, challenge = _generate_pkce_pair()
        state = self._oauth_states.create(user_id, verifier, OAUTH_STATE_TTL_SECONDS)
        return self._oauth_provider.build_authorize_url(state, challenge)


class CompleteGmailConnect:
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
        # The connection belongs to whoever *initiated* the connect (bound
        # to the state at /connect time) — never the current session, which
        # could be a different or logged-out browser by the time Google
        # redirects back. See specs/gmail-connector.md section 5.
        oauth_state = self._oauth_states.consume(state)
        if oauth_state is None:
            raise InvalidOAuthState("Unknown or expired OAuth state")

        credentials = self._oauth_provider.exchange_code(code, oauth_state.code_verifier)
        email = self._oauth_provider.get_account_email(credentials.access_token)

        connection = self._connection_repo.get_by_account(
            oauth_state.user_id, SourceType.GMAIL, email
        )
        if connection is None:
            connection = self._connection_repo.create(oauth_state.user_id, SourceType.GMAIL, email)
        else:
            # Reconnect: overwrite tokens on the existing connection rather
            # than requiring disconnect-then-reconnect.
            self._connection_repo.update_status(connection.id, ConnectionStatus.ACTIVE)

        self._credential_store.save(
            connection.id,
            credentials.access_token,
            credentials.refresh_token,
            credentials.expires_at,
        )
        return connection
