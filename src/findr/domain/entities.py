from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from findr.domain.value_objects import ChunkKind, ConnectionStatus, SourceType, UploadStatus


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
    # Human-readable label for display (e.g. "Uploaded files" for the
    # uploads connection, which has no external account at all). Separate
    # from external_account, which is the dedup key used by get_by_account()
    # and needn't be friendly on its own. None for connectors where
    # external_account is already friendly (Gmail's email address).
    display_name: str | None = None


@dataclass
class ChunkMetadata:
    """Where each field comes from: specs/upload-chunking.md §6.3."""

    chunk_index: int
    document_version: int
    content_sha256: str
    filename: str
    mime_type: str
    page_start: int | None
    page_end: int | None
    section_title: str | None
    element_types: list[str]
    languages: list[str]
    is_continuation: bool
    parser_version: str


@dataclass
class DocumentChunk:
    # Plain text: what gets embedded and shown as a snippet.
    text: str
    kind: ChunkKind
    # The table's structure as HTML, for TABLE chunks; None for TEXT.
    table_html: str | None
    metadata: ChunkMetadata


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
    # Conversation-grouping key (Gmail's threadId). Flat, not a parent/child
    # pointer — every message in a conversation shares one value. None for
    # sources without conversations (uploads) — see specs/gmail-thread-id.md.
    thread_id: str | None = None
    # Passages used for semantic search. Empty for documents that aren't
    # chunked — every source except uploads (specs/semantic-search.md §12,
    # specs/upload-chunking.md §4.2).
    chunks: list[DocumentChunk] = field(default_factory=list)


@dataclass
class UploadedFile:
    """An upload and its progress through background processing. File-
    specific metadata stays here rather than on Document, so nothing on the
    search path needs to know where a document came from. See
    specs/file-upload.md §3 and specs/upload-chunking.md §3."""

    id: int
    user_id: int
    original_filename: str
    mime_type: str
    file_size_bytes: int
    storage_path: str
    content_sha256: str
    document_version: int
    status: UploadStatus
    # Set once processing succeeds; None while pending/processing/failed.
    document_id: int | None
    # User-facing reason, for FAILED uploads.
    error: str | None
    attempts: int
    created_at: datetime
    updated_at: datetime


@dataclass
class ParsedDocument:
    """What a DocumentParser produces from one file."""

    # Full text: stored as body_text, used for keyword search and highlights.
    text: str
    # In document order; never empty.
    chunks: list[DocumentChunk]


@dataclass
class SearchHit:
    document: Document
    snippet: str
    score: float
    # Which connection's source type this hit came from (Gmail/uploaded
    # file) — lives on SearchHit rather than Document because it's a
    # property of the connection, not the document itself. Needed so the UI
    # can render a per-result source badge.
    source_type: SourceType
    # The connection's dedup key (e.g. the Gmail address) — available for
    # sources whose deep links back to the original item need it.
    external_account: str | None = None


@dataclass
class Credentials:
    access_token: str
    refresh_token: str
    expires_at: datetime
