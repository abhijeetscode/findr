import base64

from findr.adapters.outbound.gmail.gmail_mime import parse_gmail_message


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def test_parse_gmail_message_prefers_plain_text_part():
    message = {
        "id": "msg-123",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": "Q3 renewal terms"},
                {"name": "From", "value": "Priya Nair <priya@example.com>"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Date", "value": "Tue, 15 Jul 2025 10:30:00 +0000"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("Please review the terms.")}},
                {
                    "mimeType": "text/html",
                    "body": {"data": _b64("<p>Please review the <b>terms</b>.</p>")},
                },
            ],
        },
    }

    parsed = parse_gmail_message(message)

    assert parsed.external_id == "msg-123"
    assert parsed.subject == "Q3 renewal terms"
    assert parsed.sender == "Priya Nair <priya@example.com>"
    assert parsed.recipients == "me@example.com"
    assert parsed.body_text == "Please review the terms."
    assert parsed.sent_at is not None
    assert (parsed.sent_at.year, parsed.sent_at.month, parsed.sent_at.day) == (2025, 7, 15)


def test_parse_gmail_message_falls_back_to_html_when_no_plain_part():
    message = {
        "id": "msg-456",
        "payload": {
            "mimeType": "text/html",
            "headers": [{"name": "Subject", "value": "HTML only"}],
            "body": {"data": _b64("<div>Hello <strong>world</strong></div>")},
        },
    }

    parsed = parse_gmail_message(message)

    assert parsed.body_text == "Hello world"


def test_parse_gmail_message_handles_missing_headers_and_body():
    parsed = parse_gmail_message({"id": "msg-789", "payload": {}})

    assert parsed.subject is None
    assert parsed.sender is None
    assert parsed.recipients is None
    assert parsed.body_text == ""
    assert parsed.sent_at is None


def test_parse_gmail_message_ignores_unparseable_date():
    message = {
        "id": "msg-999",
        "payload": {"headers": [{"name": "Date", "value": "not a real date"}]},
    }

    parsed = parse_gmail_message(message)

    assert parsed.sent_at is None
