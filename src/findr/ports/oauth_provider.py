from typing import Protocol

from findr.domain.entities import Credentials


class OAuthProvider(Protocol):
    def build_authorize_url(self, state: str, code_challenge: str) -> str: ...
    def exchange_code(self, code: str, code_verifier: str) -> Credentials: ...
    def refresh(self, refresh_token: str) -> Credentials: ...
    def revoke(self, credentials: Credentials) -> None:
        """Takes the full Credentials, not just one token string, because
        which token a provider needs to revoke varies: Google's endpoint
        accepts either and revoking the refresh token revokes the whole
        grant, while Slack's auth.revoke specifically needs the access
        token. A provider with no revoke API (e.g. Notion) can just no-op."""
        ...
    def get_account_email(self, access_token: str) -> str:
        """The dedup key for this connection — an email for Gmail, but not
        necessarily human-friendly for other sources (a Slack
        "{team_id}:{user_id}" pair, a Notion workspace_id UUID). Used by
        get_by_account() to detect a reconnect vs. a new connection."""
        ...

    def get_display_name(self, access_token: str) -> str:
        """A human-friendly label for the connected account, shown in
        GET /sources — may differ from get_account_email()'s dedup key
        (e.g. a Slack workspace+user display name vs. its team_id:user_id
        key). For a source where the dedup key is already friendly (Gmail's
        email), this can just return the same value."""
        ...
