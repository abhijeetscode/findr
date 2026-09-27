from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

# paragraph/heading/list/etc. block types expose their text via a
# `rich_text` array under the type key; other block types (images, embeds,
# tables, code, ...) are skipped rather than fetched further.
TEXT_BLOCK_TYPES = {
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "quote",
    "callout",
}


@dataclass
class ParsedPage:
    external_id: str
    subject: str
    sender: str
    recipients: str
    body_text: str
    sent_at: datetime


def extract_title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            texts = prop.get("title") or []
            joined = "".join(t.get("plain_text", "") for t in texts)
            return joined or "Untitled"
    return "Untitled"


def extract_block_text(block: dict) -> str:
    """Returns this block's own text if it's a recognized text-bearing type,
    else "". Does not recurse into children — the connector walks the block
    tree (bounded by depth) and calls this once per block."""
    block_type = block.get("type")
    if block_type not in TEXT_BLOCK_TYPES:
        return ""
    rich_text = (block.get(block_type) or {}).get("rich_text") or []
    return "".join(t.get("plain_text", "") for t in rich_text)


def parse_timestamp(iso_ts: str) -> datetime:
    return (
        datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
    )


def parse_notion_page(
    page: dict, sender_name: str, parent_label: str, body_text: str
) -> ParsedPage:
    """Normalizes a /v1/search page result plus its already-extracted block
    text into the fields Findr indexes. sender_name/parent_label are
    caller-resolved (they require API calls); this function does no I/O."""
    return ParsedPage(
        external_id=page["id"],
        subject=extract_title(page),
        sender=sender_name,
        recipients=parent_label,
        body_text=body_text,
        sent_at=parse_timestamp(page["last_edited_time"]),
    )
