from __future__ import annotations

from datetime import timedelta

import httpx

from findr.adapters.outbound.system_clock import SystemClock
from findr.domain.entities import Credentials
from findr.domain.exceptions import SourceAuthError
from findr.ports.clock import Clock

AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
TOKEN_URL = "https://slack.com/api/oauth.v2.access"
REVOKE_URL = "https://slack.com/api/auth.revoke"
USERINFO_URL = "https://slack.com/api/openid.connect.userInfo"
TEAM_INFO_URL = "https://slack.com/api/team.info"

# Requested as user_scope (not scope, which would request bot scopes instead)
# — see specs/slack-connector.md section 6. Read-only history/identity scopes
# only; nothing that can post or modify anything.
USER_SCOPES = ",".join(
    [
        "channels:history",
        "channels:read",
        "groups:history",
        "groups:read",
        "im:history",
        "im:read",
        "mpim:history",
        "mpim:read",
        "users:read",
        "users:read.email",
        "team:read",
    ]
)
# Sign in with Slack (OIDC) — used for a stable identity independent of
# whether users:read.email gets approved.
OIDC_SCOPES = "openid profile email"

# Slack's OAuth v2 has no PKCE and (unless token rotation is enabled on the
# app) no token expiry — Credentials.expires_at still requires a value, so a
# far-future sentinel is used instead of leaving SyncSource's proactive
# refresh check unsatisfiable. See specs/slack-connector.md section 6.
_NO_EXPIRY_SENTINEL_SECONDS = 60 * 60 * 24 * 365 * 100


class SlackOAuthProvider:
    """Implements ports.oauth_provider.OAuthProvider for Slack, using the
    per-user OAuth v2 token flow (authed_user), not a bot/workspace
    install — see specs/slack-connector.md section 6 for why."""

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
        # code_challenge is unused — Slack's OAuth v2 doesn't support PKCE.
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "user_scope": USER_SCOPES,
            "scope": OIDC_SCOPES,
            "state": state,
        }
        return str(httpx.URL(AUTHORIZE_URL, params=params))

    def exchange_code(self, code: str, code_verifier: str) -> Credentials:
        # code_verifier is unused — no PKCE for Slack.
        response = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": self._redirect_uri,
            },
        )
        body = self._parse(response)
        authed_user = body.get("authed_user") or {}
        access_token = authed_user.get("access_token")
        if not access_token:
            raise SourceAuthError("Slack OAuth response had no authed_user.access_token")
        return self._credentials_from_authed_user(authed_user, access_token)

    def refresh(self, refresh_token: str) -> Credentials:
        if not refresh_token:
            raise SourceAuthError(
                "Slack token rotation is not enabled for this connection "
                "(no refresh_token was ever issued)"
            )
        response = httpx.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        body = self._parse(response)
        expires_at = self._clock.now() + timedelta(seconds=body["expires_in"])
        return Credentials(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token", refresh_token),
            expires_at=expires_at,
        )

    def revoke(self, credentials: Credentials) -> None:
        # auth.revoke needs the access token specifically, unlike Google's
        # revoke endpoint which accepts either token type.
        httpx.post(
            REVOKE_URL, headers={"Authorization": f"Bearer {credentials.access_token}"}
        )

    def get_account_email(self, access_token: str) -> str:
        """Returns the "{team_id}:{user_id}" dedup key, not a literal email —
        a Slack connection is scoped to (workspace, user), not one address.
        See specs/slack-connector.md section 5."""
        response = httpx.get(
            USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        body = self._parse(response)
        team_id = body.get("https://slack.com/team_id")
        user_id = body.get("sub")
        if not team_id or not user_id:
            raise SourceAuthError("Slack OIDC userinfo missing team_id/sub")
        return f"{team_id}:{user_id}"

    def get_display_name(self, access_token: str) -> str:
        """"{workspace_name} ({user_name})" — falls back to just the user
        name if team.info fails (e.g. team:read wasn't granted), rather than
        failing the whole connect flow over a cosmetic label."""
        userinfo = self._parse(
            httpx.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        )
        user_name = userinfo.get("name") or userinfo.get("email") or userinfo.get("sub", "unknown user")

        try:
            team_body = self._parse(
                httpx.get(TEAM_INFO_URL, headers={"Authorization": f"Bearer {access_token}"})
            )
            team_name = (team_body.get("team") or {}).get("name")
        except SourceAuthError:
            team_name = None

        return f"{team_name} ({user_name})" if team_name else user_name

    def _credentials_from_authed_user(
        self, authed_user: dict, access_token: str
    ) -> Credentials:
        expires_in = authed_user.get("expires_in")
        expires_at = (
            self._clock.now() + timedelta(seconds=expires_in)
            if expires_in is not None
            else self._clock.now() + timedelta(seconds=_NO_EXPIRY_SENTINEL_SECONDS)
        )
        # "" when rotation is disabled — refresh() rejects an empty token
        # rather than silently doing nothing.
        refresh_token = authed_user.get("refresh_token") or ""
        return Credentials(
            access_token=access_token, refresh_token=refresh_token, expires_at=expires_at
        )

    def _parse(self, response: httpx.Response) -> dict:
        if response.status_code != 200:
            raise SourceAuthError(f"Slack API request failed ({response.status_code}): {response.text}")
        body = response.json()
        if not body.get("ok", False):
            raise SourceAuthError(f"Slack API returned an error: {body.get('error')}")
        return body
