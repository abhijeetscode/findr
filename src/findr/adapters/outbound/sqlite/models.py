from datetime import datetime

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base for SQLAlchemy ORM models (adapter-layer persistence models —
    domain/ stays plain dataclasses; repositories translate between the two).

    The documents_fts virtual table + its sync triggers can't be expressed as
    an ORM model (SQLite FTS5 isn't a normal table) and stay as raw SQL in
    schema.sql, applied alongside Base.metadata.create_all() in db.py.
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

    # Plain INTEGER PRIMARY KEY (== SQLite rowid) is required: documents_fts
    # (schema.sql) is an external-content FTS5 table with content_rowid='id',
    # so this id must line up with SQLite's own rowid for the sync triggers
    # to work.
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
    thread_id: Mapped[str | None] = mapped_column(index=True)
    created_at: Mapped[datetime]
