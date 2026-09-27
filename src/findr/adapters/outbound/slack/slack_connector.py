from __future__ import annotations

import json

import httpx

from findr.adapters.outbound.slack.slack_message import label_for_conversation, parse_slack_message
from findr.domain.entities import Credentials, Document
from findr.domain.exceptions import SourceAuthError
from findr.ports.source_connector import ChangeBatch

SLACK_API_BASE = "https://slack.com/api"

# Per-conversation cap so one sync tick can't run unbounded on a channel with
# years of history — mirrors GmailConnector.MAX_MESSAGES_PER_INITIAL_SYNC,
# applied per-channel here since Slack has no single cross-conversation
# cursor. See specs/slack-connector.md section 7.
MAX_MESSAGES_PER_CHANNEL_SYNC = 200

# Errors that mean the token itself is bad, as opposed to a transient or
# per-conversation problem — only these should turn into SourceAuthError
# (which stops sync until the user reconnects). Everything else propagates
# as a plain exception so SyncSource marks the connection ERROR (retried
# next tick) instead of NEEDS_REAUTH.
_AUTH_ERRORS = {
    "invalid_auth",
    "token_revoked",
    "account_inactive",
    "missing_scope",
    "not_authed",
    "token_expired",
}


class _SlackApiError(Exception):
    """Internal: a non-auth Slack API error (e.g. rate limited, or a
    conversation the token can no longer see)."""

    def __init__(self, error: str) -> None:
        super().__init__(f"Slack API error: {error}")
        self.error = error


class SlackConnector:
    """Implements ports.source_connector.SourceConnector for Slack.

    Bound to one connection, same convention as GmailConnector. Unlike
    Gmail's single historyId, Slack has no cross-conversation cursor, so
    `cursor` here is a JSON-encoded {channel_id: last_synced_ts} map — still
    one opaque string as far as SourceConnection.sync_cursor is concerned.
    See specs/slack-connector.md section 7.
    """

    def __init__(
        self,
        user_id: int,
        connection_id: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._user_id = user_id
        self._connection_id = connection_id
        # Injectable for tests (httpx.MockTransport); real syncs use the
        # default (a real network transport).
        self._transport = transport

    def fetch_changes(self, credentials: Credentials, cursor: str | None) -> ChangeBatch:
        cursor_map: dict[str, str] = json.loads(cursor) if cursor else {}
        new_cursor_map = dict(cursor_map)
        upserts: list[Document] = []
        user_cache: dict[str, str] = {}

        with httpx.Client(
            base_url=SLACK_API_BASE,
            headers={"Authorization": f"Bearer {credentials.access_token}"},
            timeout=30.0,
            transport=self._transport,
        ) as client:
            for conv in self._list_conversations(client):
                channel_id = conv["id"]
                oldest = cursor_map.get(channel_id)
                messages, latest_ts = self._fetch_history(client, channel_id, oldest)
                if not messages:
                    continue
                label = self._label_for_conversation(client, conv, user_cache)
                for message in messages:
                    upserts.append(
                        self._to_document(client, channel_id, label, message, user_cache)
                    )
                if latest_ts is not None:
                    new_cursor_map[channel_id] = latest_ts

        return ChangeBatch(
            upserts=upserts, deleted_external_ids=[], new_cursor=json.dumps(new_cursor_map)
        )

    def _list_conversations(self, client: httpx.Client) -> list[dict]:
        conversations: list[dict] = []
        page_cursor: str | None = None
        while True:
            params: dict[str, object] = {
                "types": "public_channel,private_channel,mpim,im",
                "limit": 200,
                "exclude_archived": "true",
            }
            if page_cursor:
                params["cursor"] = page_cursor
            body = self._get(client, "conversations.list", params)
            conversations.extend(body.get("channels", []) or [])
            page_cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
            if not page_cursor:
                break
        return conversations

    def _fetch_history(
        self, client: httpx.Client, channel_id: str, oldest: str | None
    ) -> tuple[list[dict], str | None]:
        messages: list[dict] = []
        latest_ts = oldest
        page_cursor: str | None = None
        while len(messages) < MAX_MESSAGES_PER_CHANNEL_SYNC:
            params: dict[str, object] = {"channel": channel_id, "limit": 200}
            if oldest:
                params["oldest"] = oldest
            if page_cursor:
                params["cursor"] = page_cursor
            try:
                body = self._get(client, "conversations.history", params)
            except _SlackApiError:
                # E.g. the user is no longer in this conversation. Skip it
                # rather than failing the whole sync over one conversation.
                return [], latest_ts

            page_messages = [
                m
                for m in body.get("messages", []) or []
                if m.get("type") == "message" and not m.get("subtype")
            ]
            messages.extend(page_messages)
            for m in page_messages:
                if latest_ts is None or float(m["ts"]) > float(latest_ts):
                    latest_ts = m["ts"]

            page_cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
            if not body.get("has_more") or not page_cursor:
                break
        return messages[:MAX_MESSAGES_PER_CHANNEL_SYNC], latest_ts

    def _label_for_conversation(
        self, client: httpx.Client, conv: dict, cache: dict[str, str]
    ) -> str:
        other = conv.get("user")
        dm_partner_name = self._user_name(client, other, cache) if other else None
        return label_for_conversation(conv, dm_partner_name)

    def _user_name(self, client: httpx.Client, user_id: str, cache: dict[str, str]) -> str:
        if user_id in cache:
            return cache[user_id]
        try:
            body = self._get(client, "users.info", {"user": user_id})
            info = body.get("user") or {}
            name = info.get("real_name") or info.get("name") or user_id
        except _SlackApiError:
            # Deactivated/unknown user — fall back to the raw id rather than
            # failing the whole sync. See specs/slack-connector.md section 10.
            name = user_id
        cache[user_id] = name
        return name

    def _to_document(
        self,
        client: httpx.Client,
        channel_id: str,
        label: str,
        message: dict,
        user_cache: dict[str, str],
    ) -> Document:
        user_id = message.get("user")
        sender = (
            self._user_name(client, user_id, user_cache)
            if user_id
            else message.get("bot_id", "unknown")
        )
        parsed = parse_slack_message(channel_id, label, message, sender)
        return Document(
            id=0,
            user_id=self._user_id,
            connection_id=self._connection_id,
            external_id=parsed.external_id,
            subject=None,
            sender=parsed.sender,
            recipients=parsed.recipients,
            body_text=parsed.body_text,
            sent_at=parsed.sent_at,
        )

    def _get(self, client: httpx.Client, method: str, params: dict[str, object]) -> dict:
        response = client.get(f"/{method}", params=params)
        if response.status_code in (401, 403):
            raise SourceAuthError(f"Slack API HTTP {response.status_code} for {method}")
        if response.status_code != 200:
            raise _SlackApiError(f"http_{response.status_code}")
        body = response.json()
        if not body.get("ok", False):
            error = body.get("error", "unknown_error")
            if error in _AUTH_ERRORS:
                raise SourceAuthError(f"Slack API auth error for {method}: {error}")
            raise _SlackApiError(error)
        return body
