from __future__ import annotations

from datetime import timedelta

import httpx

from findr.adapters.outbound.system_clock import SystemClock
from findr.domain.entities import Credentials
from findr.domain.exceptions import SourceAuthError
from findr.ports.clock import Clock

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

SCOPES = "https://www.googleapis.com/auth/gmail.readonly openid email"


class GmailOAuthProvider:
    """Implements ports.oauth_provider.OAuthProvider for Gmail, over raw
    httpx rather than google-auth-oauthlib/google-api-python-client — see
    specs/gmail-connector.md section 6 for why (those libs are sync-only and
    have known local-dev PKCE/redirect pitfalls)."""

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

    def build_authorize_url(self, state: str, code_challenge: str) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # Both required together, or a reconnect can silently omit the
            # refresh_token on the response.
            "access_type": "offline",
            "prompt": "consent",
        }
        return str(httpx.URL(AUTHORIZE_URL, params=params))

    def exchange_code(self, code: str, code_verifier: str) -> Credentials:
        response = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": self._redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        return self._credentials_from_response(response)

    def refresh(self, refresh_token: str) -> Credentials:
        response = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        # Google usually doesn't return a new refresh_token on refresh; keep
        # the one we already have unless a new one is issued.
        return self._credentials_from_response(response, fallback_refresh_token=refresh_token)

    def revoke(self, token: str) -> None:
        httpx.post(REVOKE_URL, data={"token": token})

    def get_account_email(self, access_token: str) -> str:
        response = httpx.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        if response.status_code != 200:
            raise SourceAuthError(f"Failed to fetch Gmail account info: {response.text}")
        return response.json()["email"]

    def _credentials_from_response(
        self, response: httpx.Response, fallback_refresh_token: str | None = None
    ) -> Credentials:
        if response.status_code != 200:
            raise SourceAuthError(f"Gmail token request failed ({response.status_code}): {response.text}")
        body = response.json()
        refresh_token = body.get("refresh_token", fallback_refresh_token)
        if refresh_token is None:
            raise SourceAuthError(
                "Gmail did not return a refresh_token "
                "(missing access_type=offline / prompt=consent on the authorize request?)"
            )
        expires_at = self._clock.now() + timedelta(seconds=body["expires_in"])
        return Credentials(
            access_token=body["access_token"], refresh_token=refresh_token, expires_at=expires_at
        )
