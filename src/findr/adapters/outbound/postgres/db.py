from __future__ import annotations

from sqlalchemy import Engine, create_engine

from findr.adapters.outbound.postgres.models import Base


def create_db_engine(database_url: str) -> Engine:
    # pool_pre_ping guards against stale pooled connections (Postgres closes
    # idle connections server-side) — SQLite never had this failure mode.
    return create_engine(database_url, pool_pre_ping=True)


def init_db(engine: Engine) -> None:
    """Creates all tables. Unlike the old SQLite version, there's no raw
    schema.sql step — search moved to Elasticsearch (see
    specs/elasticsearch-search.md), so there's no FTS5-equivalent virtual
    table/triggers to apply here."""
    Base.metadata.create_all(engine)
