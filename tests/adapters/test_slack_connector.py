from datetime import datetime

import httpx

from findr.adapters.outbound.slack.slack_connector import SlackConnector
from findr.domain.entities import Credentials


def _make_transport(fixtures: dict[str, list[dict]]) -> httpx.MockTransport:
    """fixtures maps a Slack API path (e.g. "/conversations.history") to a
    list of JSON bodies returned in call order for that path."""
    queues = {path: list(items) for path, items in fixtures.items()}

    def handler(request: httpx.Request) -> httpx.Response:
        queue = queues.get(request.url.path)
        assert queue, f"unexpected or exhausted call to {request.url.path}"
        return httpx.Response(200, json=queue.pop(0))

    return httpx.MockTransport(handler)


CREDENTIALS = Credentials(access_token="xoxp-token", refresh_token="", expires_at=datetime(2030, 1, 1))


def test_fetch_changes_maps_channel_and_dm_messages_and_builds_cursor():
    transport = _make_transport(
        {
            "/api/conversations.list": [
                {
                    "ok": True,
                    "channels": [
                        {"id": "C1", "name": "general"},
                        {"id": "D1", "is_im": True, "user": "U2"},
                    ],
                }
            ],
            "/api/conversations.history": [
                {"ok": True, "messages": [{"type": "message", "ts": "1000.0001", "user": "U1", "text": "hello"}]},
                {"ok": True, "messages": [{"type": "message", "ts": "2000.0002", "user": "U2", "text": "hi"}]},
            ],
            "/api/users.info": [
                {"ok": True, "user": {"id": "U1", "real_name": "Alice"}},
                {"ok": True, "user": {"id": "U2", "real_name": "Bob"}},
            ],
        }
    )
    connector = SlackConnector(user_id=10, connection_id=1, transport=transport)

    batch = connector.fetch_changes(CREDENTIALS, cursor=None)

    assert len(batch.upserts) == 2
    channel_msg, dm_msg = batch.upserts

    assert channel_msg.external_id == "C1:1000.0001"
    assert channel_msg.sender == "Alice"
    assert channel_msg.recipients == "#general"
    assert channel_msg.body_text == "hello"

    assert dm_msg.external_id == "D1:2000.0002"
    assert dm_msg.sender == "Bob"
    assert dm_msg.recipients == "DM with Bob"
    assert dm_msg.body_text == "hi"

    assert batch.deleted_external_ids == []
    assert json_cursor(batch.new_cursor) == {"C1": "1000.0001", "D1": "2000.0002"}


def test_fetch_changes_incremental_sync_skips_already_synced_messages():
    cursor = '{"C1": "1000.0001"}'
    transport = _make_transport(
        {
            "/api/conversations.list": [{"ok": True, "channels": [{"id": "C1", "name": "general"}]}],
            "/api/conversations.history": [{"ok": True, "messages": []}],
        }
    )
    connector = SlackConnector(user_id=10, connection_id=1, transport=transport)

    batch = connector.fetch_changes(CREDENTIALS, cursor=cursor)

    assert batch.upserts == []
    assert json_cursor(batch.new_cursor) == {"C1": "1000.0001"}


def json_cursor(cursor: str) -> dict:
    import json

    return json.loads(cursor)
