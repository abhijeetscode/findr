import hashlib
import math
import re
import uuid
from collections.abc import Iterator

import pytest
from elasticsearch import Elasticsearch
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from findr.adapters.outbound.elasticsearch.es_client import EMBEDDING_DIMS, ensure_index
from findr.adapters.outbound.postgres.db import create_db_engine, init_db
from findr.adapters.outbound.postgres.models import Base

TEST_DATABASE_URL = "postgresql+psycopg://findr:findr@localhost:5432/findr_test"
TEST_ELASTICSEARCH_URL = "http://localhost:9200"


class FakeEmbeddingProvider:
    """Deterministic stand-in for the real ~1GB model: a hashed bag of
    words, L2-normalised, at the real mapping's dimension. Texts sharing no
    words get cosine similarity ~0, below ElasticsearchIndex's kNN floor — so
    keyword-only tests keep meaning "no shared words, no hit" rather than
    kNN matching every document. Only the one test that proves real
    semantic matching loads the actual model (see real_embedding_provider)."""

    def __init__(self) -> None:
        self.document_batches: list[list[str]] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_batches.append(list(texts))
        return [_hashed_bag_of_words(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return _hashed_bag_of_words(text)


def _hashed_bag_of_words(text: str) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMS
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        bucket = int(hashlib.md5(token.encode()).hexdigest(), 16) % EMBEDDING_DIMS
        vector[bucket] += 1.0
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        # Never all-zero: Elasticsearch rejects zero vectors under cosine.
        vector[0] = 1.0
        return vector
    return [v / norm for v in vector]


@pytest.fixture
def fake_embedding_provider() -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider()


class RecordingUploadQueue:
    """Stands in for the Redis/Taskiq queue: records enqueued upload ids so a
    test can run ProcessUpload for them itself."""

    def __init__(self) -> None:
        self.enqueued: list[int] = []

    def enqueue(self, upload_id: int) -> None:
        self.enqueued.append(upload_id)


@pytest.fixture
def upload_queue() -> RecordingUploadQueue:
    return RecordingUploadQueue()


@pytest.fixture(scope="session")
def real_embedding_provider():
    """The real local model — slow to load, so session-scoped and only used
    by tests that need to prove actual semantic behaviour."""
    from findr.adapters.outbound.embeddings.sentence_transformer_provider import (
        SentenceTransformerEmbeddingProvider,
    )
    from findr.config import Settings

    settings = Settings()
    return SentenceTransformerEmbeddingProvider(
        settings.embedding_model, settings.embedding_max_seq_length
    )


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
def app_env(
    monkeypatch, tmp_path, test_engine: Engine, es_index: str, upload_queue: RecordingUploadQueue
) -> Iterator[None]:
    """For tests that boot the whole app via TestClient — its lifespan
    creates its own engine/session_factory and Elasticsearch client,
    separate from the db_session/es_client fixtures above, so isolation has
    to be arranged at the database/index level instead: truncate every
    table in the shared findr_test Postgres database both before and after
    the test (before, in case a previous run left something behind; after,
    since anything this test commits through the app's own engine is a
    real commit outside any test-scoped transaction and would otherwise
    leak into whichever test runs next), and point it at a fresh, empty
    Elasticsearch index (deleted afterward by the es_index fixture).
    Uploads go to a per-test temp dir, and the app's embedding model is
    swapped for FakeEmbeddingProvider so each TestClient startup doesn't
    load the real one, and the Redis upload queue for RecordingUploadQueue
    (the `upload_queue` fixture) so no Redis is needed."""
    monkeypatch.setenv("FINDR_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("FINDR_ELASTICSEARCH_INDEX", es_index)
    monkeypatch.setenv("FINDR_UPLOAD_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setattr(
        "findr.adapters.inbound.http.app.build_embedding_provider",
        lambda settings: FakeEmbeddingProvider(),
    )

    async def start_fake_queue():
        return upload_queue

    async def stop_fake_queue():
        return None

    monkeypatch.setattr("findr.adapters.inbound.http.app.start_upload_queue", start_fake_queue)
    monkeypatch.setattr("findr.adapters.inbound.http.app.stop_upload_queue", stop_fake_queue)
    _truncate_all_tables(test_engine)
    yield
    _truncate_all_tables(test_engine)


def make_chunk(text: str, index: int = 0, **metadata_overrides):
    """A DocumentChunk with plausible metadata, for tests that need chunks
    without running the real parser."""
    from findr.domain.entities import ChunkMetadata, DocumentChunk
    from findr.domain.value_objects import ChunkKind

    metadata = dict(
        chunk_index=index,
        document_version=1,
        content_sha256="0" * 64,
        workspace_id=1,
        filename="notes.txt",
        mime_type="text/plain",
        page_start=None,
        page_end=None,
        section_title=None,
        element_types=["NarrativeText"],
        languages=["eng"],
        is_continuation=False,
        parser_version="unstructured-test/chunking-v1",
    )
    metadata.update(metadata_overrides)
    kind = metadata.pop("kind", ChunkKind.TEXT)
    table_html = metadata.pop("table_html", None)
    return DocumentChunk(
        text=text, kind=kind, table_html=table_html, metadata=ChunkMetadata(**metadata)
    )


@pytest.fixture
def chunk_factory():
    return make_chunk


def make_workspace(db, user_id: int, name: str = "Client A"):
    """A real workspace row (connections, uploads and documents all need one)."""
    from findr.adapters.outbound.postgres.workspace_repository_postgres import (
        WorkspaceRepositoryPostgres,
    )

    return WorkspaceRepositoryPostgres(db).create(user_id, name)


@pytest.fixture
def workspace_factory():
    return make_workspace


def ensure_workspace(db, user_id: int, name: str = "Client A"):
    """The user's workspace with this name, created on first use — for tests
    that just need *a* workspace to hang connections/uploads on."""
    from findr.adapters.outbound.postgres.workspace_repository_postgres import (
        WorkspaceRepositoryPostgres,
    )

    repo = WorkspaceRepositoryPostgres(db)
    return repo.get_by_name(user_id, name) or repo.create(user_id, name)
