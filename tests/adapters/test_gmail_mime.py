import base64

from findr.adapters.outbound.gmail.gmail_mime import parse_gmail_message


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def test_parse_gmail_message_prefers_plain_text_part():
    message = {
        "id": "msg-123",
        "threadId": "thread-abc",
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
    assert parsed.thread_id == "thread-abc"
    assert parsed.subject == "Q3 renewal terms"
    assert parsed.sender == "Priya Nair <priya@example.com>"
    assert parsed.recipients == "me@example.com"
    assert parsed.body_text == "Please review the terms."
    assert parsed.sent_at is not None
    assert (parsed.sent_at.year, parsed.sent_at.month, parsed.sent_at.day) == (2025, 7, 15)


def test_parse_gmail_message_falls_back_to_html_when_no_plain_part():
    message = {
        "id": "msg-456",
        "threadId": "thread-456",
        "payload": {
            "mimeType": "text/html",
            "headers": [{"name": "Subject", "value": "HTML only"}],
            "body": {"data": _b64("<div>Hello <strong>world</strong></div>")},
        },
    }

    parsed = parse_gmail_message(message)

    assert parsed.body_text == "Hello world"


def test_parse_gmail_message_handles_missing_headers_and_body():
    parsed = parse_gmail_message({"id": "msg-789", "threadId": "thread-789", "payload": {}})

    assert parsed.subject is None
    assert parsed.sender is None
    assert parsed.recipients is None
    assert parsed.body_text == ""
    assert parsed.sent_at is None


def test_parse_gmail_message_ignores_unparseable_date():
    message = {
        "id": "msg-999",
        "threadId": "thread-999",
        "payload": {"headers": [{"name": "Date", "value": "not a real date"}]},
    }

    parsed = parse_gmail_message(message)

    assert parsed.sent_at is None


# ---------- thread_id (specs/gmail-thread-id.md) ----------


def test_thread_id_is_extracted_from_top_level_field():
    message = {"id": "msg-1", "threadId": "thread-xyz", "payload": {}}

    parsed = parse_gmail_message(message)

    assert parsed.thread_id == "thread-xyz"


# ---------- quoted-text stripping (specs/gmail-quote-stripping.md) ----------


def test_strips_quoted_plain_text_after_on_wrote_delimiter():
    body = (
        "Sounds good, approved!\n\n"
        "On Tue, Jul 15, 2025 at 10:30 AM Priya Nair <priya@example.com> wrote:\n"
        "> Please review the renewal terms.\n"
        "> Let me know if you have questions."
    )
    message = {
        "id": "msg-1",
        "threadId": "thread-1",
        "payload": {"mimeType": "text/plain", "body": {"data": _b64(body)}},
    }

    parsed = parse_gmail_message(message)

    assert parsed.body_text == "Sounds good, approved!"


def test_strips_quoted_plain_text_with_leading_angle_bracket_lines():
    body = "New content here.\n> old quoted line one\n> old quoted line two"
    message = {
        "id": "msg-1",
        "threadId": "thread-1",
        "payload": {"mimeType": "text/plain", "body": {"data": _b64(body)}},
    }

    parsed = parse_gmail_message(message)

    assert parsed.body_text == "New content here."


def test_strips_quoted_html_gmail_quote_div():
    html = (
        "<div>My new reply text.</div>"
        '<div class="gmail_quote">'
        "<div>On Tue, Jul 15, 2025, Priya Nair wrote:</div>"
        "<div>Please review the renewal terms.</div>"
        "</div>"
    )
    message = {
        "id": "msg-1",
        "threadId": "thread-1",
        "payload": {"mimeType": "text/html", "body": {"data": _b64(html)}},
    }

    parsed = parse_gmail_message(message)

    assert parsed.body_text == "My new reply text."


def test_first_message_with_no_quote_markers_is_left_unchanged():
    body = "This is a brand new email with no reply history at all."
    message = {
        "id": "msg-1",
        "threadId": "thread-1",
        "payload": {"mimeType": "text/plain", "body": {"data": _b64(body)}},
    }

    parsed = parse_gmail_message(message)

    assert parsed.body_text == body
