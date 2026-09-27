from __future__ import annotations

import json

import httpx

from findr.adapters.outbound.notion.notion_page import extract_block_text, extract_title, parse_notion_page
from findr.domain.entities import Credentials, Document
from findr.domain.exceptions import SourceAuthError
from findr.ports.source_connector import ChangeBatch

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Cap on pages processed per sync tick, mirroring GmailConnector's
# per-tick bound. If the workspace's shared-page set is larger than this,
# the deletion diff is skipped for that tick (see fetch_changes) rather than
# risk a false-positive delete on a partial listing.
MAX_PAGES_PER_SYNC = 200

# Depth 2: a page's direct blocks, plus one level of nesting (e.g. a bulleted
# list's sub-items) — bounded so one deeply nested page can't turn a sync
# tick into an unbounded job. See specs/notion-connector.md section 7.
MAX_BLOCK_DEPTH = 2


class _NotionApiError(Exception):
    """Internal: a non-auth Notion API error (rate limited, not found,
    etc.) — propagates as a plain exception so SyncSource marks the
    connection ERROR (retried next tick) rather than NEEDS_REAUTH."""


class NotionConnector:
    """Implements ports.source_connector.SourceConnector for Notion.

    Bound to one connection, same convention as GmailConnector/SlackConnector.

    Notion's /v1/search has no changelog-style delta API, so `cursor` here
    is a JSON-encoded {page_id: last_edited_time} map of everything seen on
    the previous sync — still one opaque string as far as
    SourceConnection.sync_cursor is concerned. Each tick re-lists the full
    set of pages the integration can currently see, skips re-fetching block
    content for pages whose last_edited_time hasn't changed, and reports a
    page as deleted if it disappears from that full listing (this is also
    how an *unshared* page is detected — Notion's API can't tell the two
    apart). See specs/notion-connector.md sections 7 and 10.
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
        known_pages: dict[str, str] = json.loads(cursor)["pages"] if cursor else {}
        user_cache: dict[str, str] = {}
        parent_cache: dict[str, str] = {}

        with httpx.Client(
            base_url=NOTION_API_BASE,
            headers={
                "Authorization": f"Bearer {credentials.access_token}",
                "Notion-Version": NOTION_VERSION,
            },
            timeout=30.0,
            transport=self._transport,
        ) as client:
            pages, complete = self._search_pages(client)

            upserts: list[Document] = []
            new_known_pages: dict[str, str] = {}
            for page in pages:
                page_id = page["id"]
                last_edited = page["last_edited_time"]
                new_known_pages[page_id] = last_edited
                if known_pages.get(page_id) == last_edited:
                    continue  # unchanged since last sync — skip re-fetching blocks
                upserts.append(self._to_document(client, page, user_cache, parent_cache))

            deleted = (
                list(set(known_pages) - set(new_known_pages)) if complete else []
            )

        return ChangeBatch(
            upserts=upserts,
            deleted_external_ids=deleted,
            new_cursor=json.dumps({"pages": new_known_pages}),
        )

    def _search_pages(self, client: httpx.Client) -> tuple[list[dict], bool]:
        """Returns (pages, complete) — complete is False if MAX_PAGES_PER_SYNC
        was hit before exhausting the result set, in which case the caller
        must not trust the set as complete enough to diff deletions from."""
        pages: list[dict] = []
        start_cursor: str | None = None
        while len(pages) < MAX_PAGES_PER_SYNC:
            payload: dict[str, object] = {
                "filter": {"property": "object", "value": "page"},
                "sort": {"direction": "descending", "timestamp": "last_edited_time"},
                "page_size": 100,
            }
            if start_cursor:
                payload["start_cursor"] = start_cursor
            body = self._post(client, "search", payload)
            pages.extend(body.get("results", []) or [])
            if not body.get("has_more"):
                return pages, True
            start_cursor = body.get("next_cursor")
        return pages[:MAX_PAGES_PER_SYNC], False

    def _to_document(
        self,
        client: httpx.Client,
        page: dict,
        user_cache: dict[str, str],
        parent_cache: dict[str, str],
    ) -> Document:
        sender = self._user_name(client, (page.get("last_edited_by") or {}).get("id"), user_cache)
        parent_label = self._parent_label(client, page.get("parent") or {}, parent_cache)
        body_text = self._extract_page_text(client, page["id"], depth=0)
        parsed = parse_notion_page(page, sender, parent_label, body_text)
        return Document(
            id=0,
            user_id=self._user_id,
            connection_id=self._connection_id,
            external_id=parsed.external_id,
            subject=parsed.subject,
            sender=parsed.sender,
            recipients=parsed.recipients,
            body_text=parsed.body_text,
            sent_at=parsed.sent_at,
        )

    def _parent_label(self, client: httpx.Client, parent: dict, cache: dict[str, str]) -> str:
        parent_type = parent.get("type")
        if parent_type == "workspace":
            return "Workspace"
        if parent_type == "page_id":
            page_id = parent["page_id"]
            if page_id not in cache:
                try:
                    body = self._get(client, f"pages/{page_id}")
                    cache[page_id] = extract_title(body)
                except _NotionApiError:
                    cache[page_id] = "Unknown page"
            return cache[page_id]
        if parent_type == "database_id":
            database_id = parent["database_id"]
            if database_id not in cache:
                try:
                    body = self._get(client, f"databases/{database_id}")
                    texts = body.get("title") or []
                    cache[database_id] = (
                        "".join(t.get("plain_text", "") for t in texts) or "Untitled database"
                    )
                except _NotionApiError:
                    cache[database_id] = "Unknown database"
            return cache[database_id]
        return "Unknown"

    def _extract_page_text(self, client: httpx.Client, block_id: str, depth: int) -> str:
        if depth > MAX_BLOCK_DEPTH:
            return ""
        pieces: list[str] = []
        for block in self._list_block_children(client, block_id):
            text = extract_block_text(block)
            if text:
                pieces.append(text)
            if block.get("has_children") and depth < MAX_BLOCK_DEPTH:
                nested = self._extract_page_text(client, block["id"], depth + 1)
                if nested:
                    pieces.append(nested)
        return "\n".join(pieces)

    def _list_block_children(self, client: httpx.Client, block_id: str) -> list[dict]:
        results: list[dict] = []
        start_cursor: str | None = None
        while True:
            params: dict[str, object] = {"page_size": 100}
            if start_cursor:
                params["start_cursor"] = start_cursor
            try:
                body = self._get(client, f"blocks/{block_id}/children", params)
            except _NotionApiError:
                return results
            results.extend(body.get("results", []) or [])
            if not body.get("has_more"):
                break
            start_cursor = body.get("next_cursor")
        return results

    def _user_name(
        self, client: httpx.Client, user_id: str | None, cache: dict[str, str]
    ) -> str:
        if not user_id:
            return "Unknown"
        if user_id not in cache:
            try:
                body = self._get(client, f"users/{user_id}")
                cache[user_id] = body.get("name") or user_id
            except _NotionApiError:
                cache[user_id] = user_id
        return cache[user_id]

    def _get(self, client: httpx.Client, path: str, params: dict[str, object] | None = None) -> dict:
        return self._handle(client.get(f"/{path}", params=params), path)

    def _post(self, client: httpx.Client, path: str, json_body: dict) -> dict:
        return self._handle(client.post(f"/{path}", json=json_body), path)

    def _handle(self, response: httpx.Response, path: str) -> dict:
        if response.status_code in (401, 403):
            raise SourceAuthError(f"Notion API HTTP {response.status_code} for {path}")
        if response.status_code != 200:
            raise _NotionApiError(f"http_{response.status_code} for {path}")
        return response.json()
