from __future__ import annotations

from datetime import timedelta

import httpx

from findr.adapters.outbound.system_clock import SystemClock
from findr.domain.entities import Credentials
from findr.domain.exceptions import SourceAuthError
from findr.ports.clock import Clock

AUTHORIZE_URL = "https://api.notion.com/v1/oauth/authorize"
TOKEN_URL = "https://api.notion.com/v1/oauth/token"

# Notion access tokens don't expire and there's no documented refresh
# endpoint — see specs/notion-connector.md section 6. A far-future sentinel
# keeps SyncSource's proactive-refresh check from ever tripping.
_NO_EXPIRY_SENTINEL_SECONDS = 60 * 60 * 24 * 365 * 100


class NotionOAuthProvider:
    """Implements ports.oauth_provider.OAuthProvider for Notion.

    Notion's OAuth has no PKCE, no token expiry, no refresh endpoint, and no
    public revoke endpoint — refresh()/revoke() satisfy the Protocol but are
    intentionally near-no-ops. See specs/notion-connector.md section 6.

    get_account_email() deviates from a stateless lookup: Notion's
    workspace_id (the dedup key for a Notion connection — see spec section
    5) is only present in the token-exchange response, not derivable from a
    bare access token via any other endpoint. exchange_code() stashes it on
    this instance; CompleteNotionConnect calls exchange_code() then
    get_account_email() on the same provider instance within one request,
    same as CompleteGmailConnect/CompleteSlackConnect already do, so this
    holds in practice without changing the port's method shapes.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        clock: Clock | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._clock = clock or SystemClock()
        self._last_workspace_id: str | None = None
        self._last_workspace_name: str | None = None
        self._last_owner_email: str | None = None

    def build_authorize_url(self, state: str, code_challenge: str) -> str:
        # code_challenge is unused — Notion's OAuth doesn't support PKCE.
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "owner": "user",
            "state": state,
        }
        return str(httpx.URL(AUTHORIZE_URL, params=params))

    def exchange_code(self, code: str, code_verifier: str) -> Credentials:
        # code_verifier is unused — no PKCE for Notion.
        response = httpx.post(
            TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri,
            },
            auth=(self._client_id, self._client_secret),
        )
        if response.status_code != 200:
            raise SourceAuthError(
                f"Notion OAuth request failed ({response.status_code}): {response.text}"
            )
        body = response.json()
        access_token = body.get("access_token")
        workspace_id = body.get("workspace_id")
        if not access_token or not workspace_id:
            raise SourceAuthError("Notion OAuth response missing access_token/workspace_id")
        self._last_workspace_id = workspace_id
        self._last_workspace_name = body.get("workspace_name")
        # Only present if the integration has the "Read user information
        # including email addresses" capability enabled — see
        # specs/notion-connector.md section 6.
        self._last_owner_email = ((body.get("owner") or {}).get("user") or {}).get(
            "person", {}
        ).get("email")

        expires_at = self._clock.now() + timedelta(seconds=_NO_EXPIRY_SENTINEL_SECONDS)
        # No real refresh token exists for Notion; "" signals that plainly
        # rather than fabricating one. refresh() rejects it if ever called.
        return Credentials(access_token=access_token, refresh_token="", expires_at=expires_at)

    def refresh(self, refresh_token: str) -> Credentials:
        raise SourceAuthError(
            "Notion access tokens don't expire and have no refresh endpoint; "
            "refresh() should never be called for a Notion connection"
        )

    def revoke(self, credentials: Credentials) -> None:
        # No public revocation API — disconnecting is a manual step the user
        # takes in Notion's workspace settings.
        pass

    def get_account_email(self, access_token: str) -> str:
        """Returns the workspace_id dedup key (see class docstring for why
        this reads instance state from the preceding exchange_code() call
        rather than deriving it from access_token directly)."""
        if self._last_workspace_id is None:
            raise SourceAuthError(
                "get_account_email() called without a preceding exchange_code() "
                "on this NotionOAuthProvider instance"
            )
        return self._last_workspace_id

    def get_display_name(self, access_token: str) -> str:
        """"{workspace_name}" (+ owner email when the integration has that
        capability enabled) — same instance-state caveat as
        get_account_email(); see the class docstring."""
        if self._last_workspace_name is None:
            raise SourceAuthError(
                "get_display_name() called without a preceding exchange_code() "
                "on this NotionOAuthProvider instance"
            )
        if self._last_owner_email:
            return f"{self._last_workspace_name} ({self._last_owner_email})"
        return self._last_workspace_name
