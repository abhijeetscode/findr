from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import StaticPool

from findr.adapters.outbound.sqlite.models import Base

# Raw SQL for what the ORM can't express (FTS5 virtual table + sync
# triggers, added in the search-core step). Regular tables are ORM models
# in models.py, created via Base.metadata.create_all().
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def create_db_engine(database_path: str) -> Engine:
    if database_path == ":memory:":
        # StaticPool: a plain in-memory sqlite db is private to the DBAPI
        # connection that created it. Without a shared pool, every checkout
        # from the pool would open a *new* connection and see an empty,
        # unrelated database — StaticPool keeps one connection alive for the
        # engine's lifetime so all sessions see the same in-memory db.
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        engine = create_engine(
            f"sqlite:///{database_path}", connect_args={"check_same_thread": False}
        )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, connection_record):  # noqa: ARG001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        if database_path != ":memory:":
            cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    """Create ORM-mapped tables, then apply schema.sql's raw SQL (FTS5
    virtual table/triggers) via the underlying DBAPI connection's
    executescript — SQLAlchemy has no multi-statement-script equivalent."""
    Base.metadata.create_all(engine)
    schema_sql = SCHEMA_PATH.read_text()
    if schema_sql.strip():
        raw_conn = engine.raw_connection()
        try:
            raw_conn.executescript(schema_sql)
            raw_conn.commit()
        finally:
            raw_conn.close()
