from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class ParsedMessage:
    external_id: str
    sender: str
    recipients: str
    body_text: str
    sent_at: datetime


def label_for_conversation(conv: dict, dm_partner_name: str | None) -> str:
    """conv is one entry from conversations.list. dm_partner_name is the
    caller-resolved display name for a 1:1 DM's other participant
    (conv['user']) — this function does no I/O itself."""
    if conv.get("is_im"):
        return f"DM with {dm_partner_name or 'unknown'}"
    if conv.get("is_mpim"):
        return f"Group DM: {conv.get('name', conv.get('id', 'unknown'))}"
    return f"#{conv.get('name', conv.get('id', 'unknown'))}"


def parse_slack_message(
    channel_id: str, label: str, message: dict, sender_name: str
) -> ParsedMessage:
    """Normalizes one conversations.history message into the fields Findr
    indexes. sender_name is the already-resolved display name for
    message['user'] (or a bot id) — this function does no I/O."""
    ts = message["ts"]
    return ParsedMessage(
        external_id=f"{channel_id}:{ts}",
        sender=sender_name,
        recipients=label,
        body_text=message.get("text") or "",
        sent_at=datetime.fromtimestamp(float(ts), tz=UTC).replace(tzinfo=None),
    )
