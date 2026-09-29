from datetime import datetime

import httpx
import pytest

from findr.adapters.outbound.gmail.gmail_connector import GmailConnector
from findr.domain.entities import Credentials
from findr.domain.exceptions import SourceAuthError


def _make_transport(fixtures: dict[str, list[httpx.Response]]) -> httpx.MockTransport:
    """fixtures maps a Gmail API path (e.g. "/profile") to a list of
    httpx.Response objects returned in call order for that path."""
    queues = {path: list(items) for path, items in fixtures.items()}

    def handler(request: httpx.Request) -> httpx.Response:
        queue = queues.get(request.url.path)
        assert queue, f"unexpected or exhausted call to {request.url.path}"
        return queue.pop(0)

    return httpx.MockTransport(handler)


CREDENTIALS = Credentials(access_token="ya29.token", refresh_token="r", expires_at=datetime(2030, 1, 1))


def _message_response(message_id: str, thread_id: str | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": message_id,
            "threadId": thread_id or message_id,
            "payload": {
                "headers": [
                    {"name": "Subject", "value": f"Subject {message_id}"},
                    {"name": "From", "value": "a@example.com"},
                    {"name": "To", "value": "b@example.com"},
                ],
                "mimeType": "text/plain",
                "body": {},
            },
        },
    )


def test_initial_sync_skips_a_message_that_fails_to_fetch_instead_of_discarding_the_batch():
    # Regression test: one message returning a non-auth error (e.g. 403)
    # used to abort the whole batch, discarding every already-fetched
    # message. It should now be skipped and logged instead.
    transport = _make_transport(
        {
            "/gmail/v1/users/me/profile": [httpx.Response(200, json={"historyId": "1000"})],
            "/gmail/v1/users/me/messages": [
                httpx.Response(200, json={"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]})
            ],
            "/gmail/v1/users/me/messages/m1": [_message_response("m1")],
            "/gmail/v1/users/me/messages/m2": [
                httpx.Response(403, json={"error": {"message": "Forbidden"}})
            ],
            "/gmail/v1/users/me/messages/m3": [_message_response("m3")],
        }
    )
    connector = GmailConnector(user_id=10, connection_id=1, transport=transport)

    batch = connector.fetch_changes(CREDENTIALS, cursor=None)

    assert [doc.external_id for doc in batch.upserts] == ["m1", "m3"]
    assert batch.new_cursor == "1000"


def test_fetched_documents_carry_the_gmail_thread_id():
    transport = _make_transport(
        {
            "/gmail/v1/users/me/profile": [httpx.Response(200, json={"historyId": "1000"})],
            "/gmail/v1/users/me/messages": [
                httpx.Response(200, json={"messages": [{"id": "m1"}, {"id": "m2"}]})
            ],
            # m1 and m2 are two replies in the same conversation — same threadId.
            "/gmail/v1/users/me/messages/m1": [_message_response("m1", thread_id="thread-1")],
            "/gmail/v1/users/me/messages/m2": [_message_response("m2", thread_id="thread-1")],
        }
    )
    connector = GmailConnector(user_id=10, connection_id=1, transport=transport)

    batch = connector.fetch_changes(CREDENTIALS, cursor=None)

    assert [doc.thread_id for doc in batch.upserts] == ["thread-1", "thread-1"]


def test_rate_limited_message_is_retried_once_after_a_cooldown():
    rate_limit_body = {
        "error": {
            "errors": [{"reason": "rateLimitExceeded", "domain": "usageLimits"}],
            "message": "Quota exceeded",
        }
    }
    transport = _make_transport(
        {
            "/gmail/v1/users/me/profile": [httpx.Response(200, json={"historyId": "1000"})],
            "/gmail/v1/users/me/messages": [
                httpx.Response(200, json={"messages": [{"id": "m1"}]})
            ],
            "/gmail/v1/users/me/messages/m1": [
                httpx.Response(403, json=rate_limit_body),
                _message_response("m1"),  # succeeds on the retry after cooldown
            ],
        }
    )
    sleeps: list[float] = []
    connector = GmailConnector(
        user_id=10, connection_id=1, transport=transport, sleep=sleeps.append
    )

    batch = connector.fetch_changes(CREDENTIALS, cursor=None)

    assert [doc.external_id for doc in batch.upserts] == ["m1"]
    assert sleeps == [60]  # RATE_LIMIT_COOLDOWN_SECONDS, paused exactly once


def test_rate_limit_retry_still_gives_up_if_the_retry_also_fails():
    rate_limit_body = {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}
    transport = _make_transport(
        {
            "/gmail/v1/users/me/profile": [httpx.Response(200, json={"historyId": "1000"})],
            "/gmail/v1/users/me/messages": [
                httpx.Response(200, json={"messages": [{"id": "m1"}]})
            ],
            "/gmail/v1/users/me/messages/m1": [
                httpx.Response(403, json=rate_limit_body),
                httpx.Response(403, json=rate_limit_body),
            ],
        }
    )
    sleeps: list[float] = []
    connector = GmailConnector(
        user_id=10, connection_id=1, transport=transport, sleep=sleeps.append
    )

    batch = connector.fetch_changes(CREDENTIALS, cursor=None)

    assert batch.upserts == []  # skipped, not raised — one retry only, then give up
    assert sleeps == [60]


def test_initial_sync_still_raises_on_a_401_instead_of_skipping():
    transport = _make_transport(
        {
            "/gmail/v1/users/me/profile": [httpx.Response(200, json={"historyId": "1000"})],
            "/gmail/v1/users/me/messages": [
                httpx.Response(200, json={"messages": [{"id": "m1"}]})
            ],
            "/gmail/v1/users/me/messages/m1": [
                httpx.Response(401, json={"error": {"message": "Unauthorized"}})
            ],
        }
    )
    connector = GmailConnector(user_id=10, connection_id=1, transport=transport)

    with pytest.raises(SourceAuthError):
        connector.fetch_changes(CREDENTIALS, cursor=None)
