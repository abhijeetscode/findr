from __future__ import annotations

import httpx

from findr.adapters.outbound.gmail.gmail_mime import parse_gmail_message
from findr.domain.entities import Credentials, Document
from findr.domain.exceptions import SourceAuthError, SourceCursorExpired
from findr.ports.source_connector import ChangeBatch

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# Cap on how many messages a single initial sync fetches, so a huge mailbox
# doesn't turn one sync tick into an unbounded-length job. Later ticks catch
# up incrementally via history.list.
MAX_MESSAGES_PER_INITIAL_SYNC = 200


class GmailConnector:
    """Implements ports.source_connector.SourceConnector for Gmail.

    Bound to one connection (user_id/connection_id are stamped onto every
    Document it returns) — the caller constructs a fresh instance per
    connection, same as any other SourceConnector implementation would.
    """

    def __init__(self, user_id: int, connection_id: int) -> None:
        self._user_id = user_id
        self._connection_id = connection_id

    def fetch_changes(self, credentials: Credentials, cursor: str | None) -> ChangeBatch:
        with httpx.Client(
            base_url=GMAIL_API_BASE,
            headers={"Authorization": f"Bearer {credentials.access_token}"},
            timeout=30.0,
        ) as client:
            if cursor is None:
                return self._initial_sync(client)
            return self._incremental_sync(client, cursor)

    def _initial_sync(self, client: httpx.Client) -> ChangeBatch:
        # Captured *before* listing starts, so mail arriving mid-sync isn't
        # missed — any overlap with the next incremental sync is handled by
        # upsert dedup on (connection_id, external_id).
        profile = self._get(client, "/profile")
        start_history_id = str(profile["historyId"])

        message_ids: list[str] = []
        page_token: str | None = None
        while len(message_ids) < MAX_MESSAGES_PER_INITIAL_SYNC:
            params: dict[str, object] = {"maxResults": 100}
            if page_token:
                params["pageToken"] = page_token
            page = self._get(client, "/messages", params=params)
            message_ids.extend(m["id"] for m in page.get("messages", []) or [])
            page_token = page.get("nextPageToken")
            if not page_token:
                break

        upserts = [
            self._fetch_document(client, mid)
            for mid in message_ids[:MAX_MESSAGES_PER_INITIAL_SYNC]
        ]
        return ChangeBatch(upserts=upserts, deleted_external_ids=[], new_cursor=start_history_id)

    def _incremental_sync(self, client: httpx.Client, cursor: str) -> ChangeBatch:
        upserts: list[Document] = []
        deleted: list[str] = []
        new_cursor = cursor
        page_token: str | None = None

        while True:
            params: dict[str, object] = {
                "startHistoryId": cursor,
                "historyTypes": ["messageAdded", "messageDeleted", "labelAdded"],
            }
            if page_token:
                params["pageToken"] = page_token

            response = client.get("/history", params=params)
            if response.status_code == 404:
                # historyId too old (Gmail retains ~7 days) — caller falls
                # back to a full resync.
                raise SourceCursorExpired(f"Gmail history cursor {cursor!r} has expired")
            if response.status_code == 401:
                raise SourceAuthError("Gmail API returned 401 for history.list")
            response.raise_for_status()
            body = response.json()

            for record in body.get("history", []) or []:
                for added in record.get("messagesAdded", []) or []:
                    upserts.append(self._fetch_document(client, added["message"]["id"]))
                for removed in record.get("messagesDeleted", []) or []:
                    deleted.append(removed["message"]["id"])
                for label_change in record.get("labelsAdded", []) or []:
                    if set(label_change.get("labelIds", [])) & {"TRASH", "SPAM"}:
                        deleted.append(label_change["message"]["id"])

            new_cursor = str(body.get("historyId", new_cursor))
            page_token = body.get("nextPageToken")
            if not page_token:
                break

        return ChangeBatch(upserts=upserts, deleted_external_ids=deleted, new_cursor=new_cursor)

    def _fetch_document(self, client: httpx.Client, message_id: str) -> Document:
        raw = self._get(client, f"/messages/{message_id}", params={"format": "full"})
        parsed = parse_gmail_message(raw)
        return Document(
            id=0,  # ignored by the repository on insert
            user_id=self._user_id,
            connection_id=self._connection_id,
            external_id=parsed.external_id,
            subject=parsed.subject,
            sender=parsed.sender,
            recipients=parsed.recipients,
            body_text=parsed.body_text,
            sent_at=parsed.sent_at,
        )

    def _get(
        self, client: httpx.Client, path: str, params: dict[str, object] | None = None
    ) -> dict:
        response = client.get(path, params=params)
        if response.status_code == 401:
            raise SourceAuthError(f"Gmail API returned 401 for {path}")
        response.raise_for_status()
        return response.json()
