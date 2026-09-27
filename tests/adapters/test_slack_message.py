from datetime import datetime

from findr.adapters.outbound.slack.slack_message import label_for_conversation, parse_slack_message


def test_label_for_public_channel():
    conv = {"id": "C123", "name": "general", "is_channel": True}
    assert label_for_conversation(conv, None) == "#general"


def test_label_for_dm_uses_resolved_partner_name():
    conv = {"id": "D123", "is_im": True, "user": "U456"}
    assert label_for_conversation(conv, "Ada Lovelace") == "DM with Ada Lovelace"


def test_label_for_dm_falls_back_when_partner_name_unresolved():
    conv = {"id": "D123", "is_im": True, "user": "U456"}
    assert label_for_conversation(conv, None) == "DM with unknown"


def test_label_for_group_dm():
    conv = {"id": "G123", "is_mpim": True, "name": "mpdm-a--b-1"}
    assert label_for_conversation(conv, None) == "Group DM: mpdm-a--b-1"


def test_parse_slack_message_maps_fields_and_builds_external_id():
    message = {"ts": "1700000000.000100", "user": "U1", "text": "hello there"}

    parsed = parse_slack_message("C123", "#general", message, sender_name="Ada")

    assert parsed.external_id == "C123:1700000000.000100"
    assert parsed.sender == "Ada"
    assert parsed.recipients == "#general"
    assert parsed.body_text == "hello there"
    assert parsed.sent_at == datetime(2023, 11, 14, 22, 13, 20, 100)


def test_parse_slack_message_handles_missing_text():
    message = {"ts": "1700000000.000100", "user": "U1"}

    parsed = parse_slack_message("C123", "#general", message, sender_name="Ada")

    assert parsed.body_text == ""
