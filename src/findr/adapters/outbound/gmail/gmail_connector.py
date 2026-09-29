from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

from findr.adapters.outbound.gmail.gmail_mime import parse_gmail_message
from findr.domain.entities import Credentials, Document
from findr.domain.exceptions import SourceAuthError, SourceCursorExpired
from findr.ports.source_connector import ChangeBatch

logger = logging.getLogger(__name__)

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# Cap on how many messages a single initial sync fetches, so a huge mailbox
# doesn't turn one sync tick into an unbounded-length job. Later ticks catch
# up incrementally via history.list.
MAX_MESSAGES_PER_INITIAL_SYNC = 200

# 403 reasons that mean "you're over a short-lived quota window, try again
# shortly" — worth one retry after a pause. dailyLimitExceeded is
# deliberately excluded: that quota only resets the next day, so retrying
# in-process can't help and would just waste the rest of this sync tick.
_RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}

# Gmail's per-user quota (e.g. totalQueryCostPerMinutePerUser) is a rolling
# one-minute window, so a short backoff wouldn't reliably clear it — this
# pauses once per rate-limited call, long enough for the window to roll over.
RATE_LIMIT_COOLDOWN_SECONDS = 60


class GmailConnector:
    """Implements ports.source_connector.SourceConnector for Gmail.

    Bound to one connection (user_id/connection_id are stamped onto every
    Document it returns) — the caller constructs a fresh instance per
    connection, same as any other SourceConnector implementation would.
    """

    def __init__(
        self,
        user_id: int,
        connection_id: int,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._user_id = user_id
        self._connection_id = connection_id
        # Injectable for tests (httpx.MockTransport); real syncs use the
        # default (a real network transport).
        self._transport = transport
        # Injectable so tests can assert on rate-limit backoff without
        # actually waiting RATE_LIMIT_COOLDOWN_SECONDS in real time.
        self._sleep = sleep

    def fetch_changes(self, credentials: Credentials, cursor: str | None) -> ChangeBatch:
        with httpx.Client(
            base_url=GMAIL_API_BASE,
            headers={"Authorization": f"Bearer {credentials.access_token}"},
            timeout=30.0,
            transport=self._transport,
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

        upserts: list[Document] = []
        skipped = 0
        for mid in message_ids[:MAX_MESSAGES_PER_INITIAL_SYNC]:
            document = self._fetch_document_safe(client, mid)
            if document is not None:
                upserts.append(document)
            else:
                skipped += 1
        if skipped:
            # A summary line so a fully (or mostly) failed sync is obvious
            # without scrolling through every per-message warning above —
            # SyncSource still marks this connection ACTIVE if nothing raised,
            # even if every message was skipped, so this is the only signal.
            logger.warning(
                "Gmail initial sync for connection %s: fetched %d message(s), "
                "skipped %d after fetch errors",
                self._connection_id,
                len(upserts),
                skipped,
            )
        return ChangeBatch(upserts=upserts, deleted_external_ids=[], new_cursor=start_history_id)

    def _incremental_sync(self, client: httpx.Client, cursor: str) -> ChangeBatch:
        upserts: list[Document] = []
        deleted: list[str] = []
        new_cursor = cursor
        page_token: str | None = None
        skipped = 0

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
                    document = self._fetch_document_safe(client, added["message"]["id"])
                    if document is not None:
                        upserts.append(document)
                    else:
                        skipped += 1
                for removed in record.get("messagesDeleted", []) or []:
                    deleted.append(removed["message"]["id"])
                for label_change in record.get("labelsAdded", []) or []:
                    if set(label_change.get("labelIds", [])) & {"TRASH", "SPAM"}:
                        deleted.append(label_change["message"]["id"])

            new_cursor = str(body.get("historyId", new_cursor))
            page_token = body.get("nextPageToken")
            if not page_token:
                break

        if skipped:
            logger.warning(
                "Gmail incremental sync for connection %s: fetched %d message(s), "
                "skipped %d after fetch errors",
                self._connection_id,
                len(upserts),
                skipped,
            )
        return ChangeBatch(upserts=upserts, deleted_external_ids=deleted, new_cursor=new_cursor)

    def _fetch_document_safe(self, client: httpx.Client, message_id: str) -> Document | None:
        """Wraps _fetch_document so one bad message (e.g. a transient or
        message-specific 403) can't discard an entire otherwise-successful
        batch of up to MAX_MESSAGES_PER_INITIAL_SYNC fetches — the message is
        logged and skipped instead. SourceAuthError still propagates: that
        means the whole connection's token is bad, not just this message."""
        try:
            return self._fetch_document(client, message_id)
        except SourceAuthError:
            raise
        except Exception:
            logger.warning(
                "Skipping Gmail message %s after a fetch failure", message_id, exc_info=True
            )
            return None

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
            thread_id=parsed.thread_id,
        )

    def _get(
        self, client: httpx.Client, path: str, params: dict[str, object] | None = None
    ) -> dict:
        response = client.get(path, params=params)
        if response.status_code == 401:
            raise SourceAuthError(f"Gmail API returned 401 for {path}")
        if response.status_code == 403 and self._rate_limit_reason(response) is not None:
            reason = self._rate_limit_reason(response)
            logger.warning(
                "Gmail rate limit (%s) for %s — pausing %ds before one retry",
                reason,
                path,
                RATE_LIMIT_COOLDOWN_SECONDS,
            )
            self._sleep(RATE_LIMIT_COOLDOWN_SECONDS)
            response = client.get(path, params=params)
            if response.status_code == 401:
                raise SourceAuthError(f"Gmail API returned 401 for {path}")
        if response.status_code >= 400:
            # Google's error body (e.g. {"error": {"errors": [{"reason":
            # "insufficientPermissions", ...}]}}) says *why* far more
            # precisely than the generic HTTP status text — surface it in
            # the exception so logs/last_error show the real cause.
            raise httpx.HTTPStatusError(
                f"Gmail API {response.status_code} for {path}: {response.text}",
                request=response.request,
                response=response,
            )
        return response.json()

    def _rate_limit_reason(self, response: httpx.Response) -> str | None:
        try:
            errors = response.json().get("error", {}).get("errors", [])
        except ValueError:
            return None
        for error in errors:
            reason = error.get("reason")
            if reason in _RATE_LIMIT_REASONS:
                return reason
        return None
