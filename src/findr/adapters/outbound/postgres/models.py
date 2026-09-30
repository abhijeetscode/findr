from datetime import datetime

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base for SQLAlchemy ORM models (adapter-layer persistence models —
    domain/ stays plain dataclasses; repositories translate between the two).
    """


class UserModel(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True, index=True)
    password_hash: Mapped[str]
    created_at: Mapped[datetime]


class SessionModel(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]


class OAuthStateModel(Base):
    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    code_verifier: Mapped[str]
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]


class SourceConnectionModel(Base):
    __tablename__ = "source_connections"
    __table_args__ = (UniqueConstraint("user_id", "source_type", "external_account"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    source_type: Mapped[str]
    external_account: Mapped[str | None]
    display_name: Mapped[str | None]
    status: Mapped[str] = mapped_column(index=True)
    access_token_enc: Mapped[bytes | None]
    refresh_token_enc: Mapped[bytes | None]
    token_expires_at: Mapped[datetime | None]
    sync_cursor: Mapped[str | None]
    last_synced_at: Mapped[datetime | None]
    last_error: Mapped[str | None]
    created_at: Mapped[datetime]


class DocumentModel(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("connection_id", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("source_connections.id"), index=True
    )
    external_id: Mapped[str]
    subject: Mapped[str | None]
    sender: Mapped[str | None]
    recipients: Mapped[str | None]
    body_text: Mapped[str | None]
    sent_at: Mapped[datetime | None]
    # Gmail conversation-grouping key — see specs/gmail-thread-id.md. None
    # for connectors that don't populate it (Slack/Notion, out of scope there).
    thread_id: Mapped[str | None] = mapped_column(index=True)
    created_at: Mapped[datetime]


class UploadedFileModel(Base):
    """File-specific metadata for an uploaded document — its own table rather
    than nullable columns on documents, see specs/file-upload.md §3."""

    __tablename__ = "uploaded_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id"), unique=True, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    original_filename: Mapped[str]
    mime_type: Mapped[str]
    file_size_bytes: Mapped[int]
    storage_path: Mapped[str]
    created_at: Mapped[datetime]
