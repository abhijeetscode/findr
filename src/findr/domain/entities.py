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


@dataclass
class Credentials:
    access_token: str
    refresh_token: str
    expires_at: datetime
