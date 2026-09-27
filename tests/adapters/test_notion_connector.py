import json
from datetime import datetime

import httpx

from findr.adapters.outbound.notion.notion_connector import NotionConnector
from findr.domain.entities import Credentials


def _make_transport(fixtures: dict[str, list[dict]]) -> httpx.MockTransport:
    """fixtures maps a Notion API path (e.g. "/v1/search") to a list of JSON
    bodies returned in call order for that path."""
    queues = {path: list(items) for path, items in fixtures.items()}

    def handler(request: httpx.Request) -> httpx.Response:
        queue = queues.get(request.url.path)
        assert queue, f"unexpected or exhausted call to {request.url.path}"
        return httpx.Response(200, json=queue.pop(0))

    return httpx.MockTransport(handler)


CREDENTIALS = Credentials(access_token="secret_token", refresh_token="", expires_at=datetime(2200, 1, 1))


def _page(page_id: str, title: str, last_edited_time: str) -> dict:
    return {
        "id": page_id,
        "last_edited_time": last_edited_time,
        "last_edited_by": {"id": "user-1"},
        "parent": {"type": "workspace"},
        "properties": {"Name": {"type": "title", "title": [{"plain_text": title}]}},
    }


def _blocks(text: str) -> dict:
    return {
        "results": [
            {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": text}]}, "has_children": False}
        ],
        "has_more": False,
    }


def test_fetch_changes_maps_pages_and_extracts_block_text():
    transport = _make_transport(
        {
            "/v1/search": [
                {
                    "results": [
                        _page("page-1", "Doc One", "2023-11-14T22:13:20.000Z"),
                        _page("page-2", "Doc Two", "2023-11-15T10:00:00.000Z"),
                    ],
                    "has_more": False,
                }
            ],
            "/v1/blocks/page-1/children": [_blocks("Hello")],
            "/v1/blocks/page-2/children": [_blocks("World")],
            "/v1/users/user-1": [{"name": "Alice"}],
        }
    )
    connector = NotionConnector(user_id=10, connection_id=1, transport=transport)

    batch = connector.fetch_changes(CREDENTIALS, cursor=None)

    assert len(batch.upserts) == 2
    first, second = batch.upserts
    assert first.external_id == "page-1"
    assert first.subject == "Doc One"
    assert first.sender == "Alice"
    assert first.recipients == "Workspace"
    assert first.body_text == "Hello"
    assert second.external_id == "page-2"
    assert second.body_text == "World"
    assert batch.deleted_external_ids == []

    cursor = json.loads(batch.new_cursor)
    assert cursor == {
        "pages": {"page-1": "2023-11-14T22:13:20.000Z", "page-2": "2023-11-15T10:00:00.000Z"}
    }


def test_fetch_changes_skips_unchanged_pages_and_detects_deletions():
    prior_cursor = json.dumps(
        {"pages": {"page-1": "2023-11-14T22:13:20.000Z", "page-3": "2023-01-01T00:00:00.000Z"}}
    )
    transport = _make_transport(
        {
            "/v1/search": [
                {
                    "results": [
                        # page-1 unchanged (same last_edited_time as cursor)
                        _page("page-1", "Doc One", "2023-11-14T22:13:20.000Z"),
                        # page-2 is new
                        _page("page-2", "Doc Two", "2023-11-15T10:00:00.000Z"),
                        # page-3 no longer appears — was unshared/deleted
                    ],
                    "has_more": False,
                }
            ],
            # page-1's blocks are NOT fetched — its content is unchanged.
            "/v1/blocks/page-2/children": [_blocks("World")],
            "/v1/users/user-1": [{"name": "Alice"}],
        }
    )
    connector = NotionConnector(user_id=10, connection_id=1, transport=transport)

    batch = connector.fetch_changes(CREDENTIALS, cursor=prior_cursor)

    assert [d.external_id for d in batch.upserts] == ["page-2"]
    assert batch.deleted_external_ids == ["page-3"]
    cursor = json.loads(batch.new_cursor)
    assert cursor == {
        "pages": {"page-1": "2023-11-14T22:13:20.000Z", "page-2": "2023-11-15T10:00:00.000Z"}
    }
