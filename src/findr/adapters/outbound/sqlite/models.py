from datetime import datetime

from sqlalchemy import ForeignKey
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
