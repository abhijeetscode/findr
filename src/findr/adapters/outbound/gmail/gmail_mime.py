from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# Quoted-reply detection (specs/gmail-quote-stripping.md). Heuristic, not a
# guaranteed-correct parser — see that spec's Edge cases for known trade-offs.
_QUOTE_DELIMITER_RE = re.compile(
    r"^\s*On .+ wrote:\s*$"
    r"|^\s*-{2,}\s*Original Message\s*-{2,}\s*$"
    r"|^>",
    re.IGNORECASE | re.MULTILINE,
)
_GMAIL_QUOTE_HTML_RE = re.compile(r'<div class="gmail_quote".*', re.IGNORECASE | re.DOTALL)


@dataclass
class ParsedMessage:
    external_id: str
    subject: str | None
    sender: str | None
    recipients: str | None
    body_text: str
    sent_at: datetime | None
    thread_id: str  # always present on a Gmail message resource


def parse_gmail_message(message: dict[str, Any]) -> ParsedMessage:
    """Normalizes a Gmail API `messages.get(format=full)` response into the
    plain fields Findr indexes. Attachments are never touched — only header
    fields and the text body."""
    payload = message.get("payload", {}) or {}
    headers = payload.get("headers", []) or []

    return ParsedMessage(
        external_id=message["id"],
        subject=_header(headers, "Subject"),
        sender=_header(headers, "From"),
        recipients=_header(headers, "To"),
        body_text=_extract_body_text(payload),
        sent_at=_parse_date(_header(headers, "Date")),
        thread_id=message["threadId"],
    )


def _header(headers: list[dict[str, str]], name: str) -> str | None:
    name_lower = name.lower()
    for header in headers:
        if header.get("name", "").lower() == name_lower:
            return header.get("value")
    return None


def _parse_date(date_header: str | None) -> datetime | None:
    if not date_header:
        return None
    try:
        parsed = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _decode_base64url(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _find_part_body(payload: dict[str, Any], mime_type: str) -> str | None:
    if payload.get("mimeType") == mime_type:
        data = (payload.get("body") or {}).get("data")
        return _decode_base64url(data) if data else None
    for part in payload.get("parts") or []:
        found = _find_part_body(part, mime_type)
        if found is not None:
            return found
    return None


def _strip_html(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _strip_quoted_plain(text: str) -> str:
    """Truncates at the first quoted-reply delimiter (specs/gmail-quote-stripping.md
    §2) — everything from there on is prior messages in the thread, not this
    message's own content."""
    match = _QUOTE_DELIMITER_RE.search(text)
    return text[: match.start()].rstrip() if match else text


def _strip_quoted_html(html: str) -> str:
    """Removes Gmail's own `<div class="gmail_quote">` wrapper (and
    everything after it) before tag-stripping — a reliable, Gmail-specific
    signal, not a guess about formatting."""
    match = _GMAIL_QUOTE_HTML_RE.search(html)
    return html[: match.start()] if match else html


def _extract_body_text(payload: dict[str, Any]) -> str:
    """Prefer text/plain; fall back to a naive tag-strip of text/html. Quoted
    reply content is stripped from whichever part is used — see
    specs/gmail-quote-stripping.md."""
    plain = _find_part_body(payload, "text/plain")
    if plain is not None:
        return _strip_quoted_plain(plain)
    html = _find_part_body(payload, "text/html")
    if html is not None:
        return _strip_html(_strip_quoted_html(html))
    return ""
