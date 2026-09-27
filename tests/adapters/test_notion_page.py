from datetime import datetime

from findr.adapters.outbound.notion.notion_page import (
    extract_block_text,
    extract_title,
    parse_notion_page,
    parse_timestamp,
)


def test_extract_title_from_title_property():
    page = {
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "Roadmap "}, {"plain_text": "Q4"}]}
        }
    }
    assert extract_title(page) == "Roadmap Q4"


def test_extract_title_defaults_to_untitled_when_empty():
    page = {"properties": {"Name": {"type": "title", "title": []}}}
    assert extract_title(page) == "Untitled"


def test_extract_title_defaults_to_untitled_when_no_title_property():
    page = {"properties": {"Status": {"type": "select", "select": None}}}
    assert extract_title(page) == "Untitled"


def test_extract_block_text_for_recognized_block_type():
    block = {
        "type": "paragraph",
        "paragraph": {"rich_text": [{"plain_text": "Hello "}, {"plain_text": "world"}]},
    }
    assert extract_block_text(block) == "Hello world"


def test_extract_block_text_ignores_unrecognized_block_type():
    block = {"type": "image", "image": {"file": {"url": "https://example.com/x.png"}}}
    assert extract_block_text(block) == ""


def test_parse_timestamp_converts_iso_to_naive_utc():
    assert parse_timestamp("2023-11-14T22:13:20.000Z") == datetime(2023, 11, 14, 22, 13, 20)


def test_parse_notion_page_maps_fields():
    page = {
        "id": "page-123",
        "last_edited_time": "2023-11-14T22:13:20.000Z",
        "properties": {"Name": {"type": "title", "title": [{"plain_text": "My Page"}]}},
    }

    parsed = parse_notion_page(
        page, sender_name="Ada Lovelace", parent_label="Workspace", body_text="some content"
    )

    assert parsed.external_id == "page-123"
    assert parsed.subject == "My Page"
    assert parsed.sender == "Ada Lovelace"
    assert parsed.recipients == "Workspace"
    assert parsed.body_text == "some content"
    assert parsed.sent_at == datetime(2023, 11, 14, 22, 13, 20)
