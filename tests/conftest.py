import uuid
from collections.abc import Iterator

import pytest
from elasticsearch import Elasticsearch
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from findr.adapters.outbound.elasticsearch.es_client import ensure_index
from findr.adapters.outbound.postgres.db import create_db_engine, init_db
from findr.adapters.outbound.postgres.models import Base

TEST_DATABASE_URL = "postgresql+psycopg://findr:findr@localhost:5432/findr_test"
TEST_ELASTICSEARCH_URL = "http://localhost:9200"


@pytest.fixture(scope="session")
def test_engine() -> Iterator[Engine]:
    engine = create_db_engine(TEST_DATABASE_URL)
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session_factory(test_engine: Engine) -> Iterator[sessionmaker]:
    """A sessionmaker whose sessions all share one connection-level
    transaction that's rolled back at teardown — Postgres's replacement for
    the isolation a fresh SQLite :memory: engine gave each test for free.
    join_transaction_mode="create_savepoint" lets adapter code call
    session.commit() freely (as it does in real use): each commit only ends
    a SAVEPOINT nested inside the outer transaction, so nothing survives the
    rollback below."""
    connection = test_engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield factory
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture
def db_session(db_session_factory: sessionmaker) -> Iterator[Session]:
    session = db_session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def es_client() -> Iterator[Elasticsearch]:
    client = Elasticsearch(TEST_ELASTICSEARCH_URL)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def es_index(es_client: Elasticsearch) -> Iterator[str]:
    """A fresh, uniquely-named index, pre-created with the real production
    mapping (see es_client.ensure_index) rather than left for Elasticsearch
    to infer dynamically — dynamic mapping would type external_id/
    connection_id as text/long instead of keyword, silently breaking the
    exact-match term queries delete_documents relies on. Deleted at
    teardown so repeated test runs don't accumulate indices."""
    index_name = f"findr_test_{uuid.uuid4().hex[:12]}"
    ensure_index(es_client, index_name)
    yield index_name
    es_client.indices.delete(index=index_name, ignore_unavailable=True)


def _truncate_all_tables(engine: Engine) -> None:
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE'))


@pytest.fixture
def app_env(monkeypatch, test_engine: Engine, es_index: str) -> Iterator[None]:
    """For tests that boot the whole app via TestClient — its lifespan
    creates its own engine/session_factory and Elasticsearch client,
    separate from the db_session/es_client fixtures above, so isolation has
    to be arranged at the database/index level instead: truncate every
    table in the shared findr_test Postgres database both before and after
    the test (before, in case a previous run left something behind; after,
    since anything this test commits through the app's own engine is a
    real commit outside any test-scoped transaction and would otherwise
    leak into whichever test runs next), and point it at a fresh, empty
    Elasticsearch index (deleted afterward by the es_index fixture)."""
    monkeypatch.setenv("FINDR_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("FINDR_ELASTICSEARCH_INDEX", es_index)
    _truncate_all_tables(test_engine)
    yield
    _truncate_all_tables(test_engine)
