from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from findr.domain.value_objects import ConnectionStatus, SourceType


@dataclass
class User:
    id: int
    email: str
    password_hash: str
    created_at: datetime


@dataclass
class SourceConnection:
    id: int
    user_id: int
    source_type: SourceType
    external_account: str | None
    status: ConnectionStatus
    sync_cursor: str | None
    last_synced_at: datetime | None
    last_error: str | None
    created_at: datetime
    # Human-readable label for display (e.g. "acme-corp (ada@acme.com)" for
    # Slack, a workspace name for Notion). Separate from external_account,
    # which is the dedup key used by get_by_account() and may not be
    # friendly on its own (a Slack "{team_id}:{user_id}" pair, a Notion
    # workspace_id UUID). None for connectors where external_account is
    # already friendly (Gmail's email address).
    display_name: str | None = None


@dataclass
class Document:
    id: int
    user_id: int
    connection_id: int
    external_id: str
    subject: str | None
    sender: str | None
    recipients: str | None
    body_text: str | None
    sent_at: datetime | None


@dataclass
class SearchHit:
    document: Document
    snippet: str
    score: float
    # Which connection's source type this hit came from (Gmail/Slack/
    # Notion) — lives on SearchHit rather than Document because it's a
    # property of the connection, not the document itself. Needed so the UI
    # can render a per-result source badge.
    source_type: SourceType
    # The connection's dedup key (Gmail email, Slack "{team_id}:{user_id}",
    # Notion workspace_id) — needed to build a deep link back to the
    # original item for some sources (e.g. Slack needs the team_id).
    external_account: str | None = None


@dataclass
class Credentials:
    access_token: str
    refresh_token: str
    expires_at: datetime
