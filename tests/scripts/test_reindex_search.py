import importlib.util
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from findr.adapters.outbound.elasticsearch.search_index_elasticsearch import ElasticsearchIndex
from findr.adapters.outbound.postgres.document_repository_postgres import (
    DocumentRepositoryPostgres,
)
from findr.adapters.outbound.postgres.source_connection_repo_postgres import (
    SourceConnectionRepositoryPostgres,
)
from findr.adapters.outbound.postgres.user_repository_postgres import UserRepositoryPostgres
from findr.application.search.search_documents import SearchDocuments
from findr.domain.entities import Document
from findr.domain.value_objects import SourceType
from conftest import ensure_workspace

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "reindex_search.py"
_spec = importlib.util.spec_from_file_location("reindex_search", _SCRIPT_PATH)
reindex_search = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reindex_search)


def test_reindex_search_backfills_postgres_documents_into_elasticsearch(
    app_env, monkeypatch, test_engine, es_client, es_index, fake_embedding_provider
):
    monkeypatch.setattr(
        reindex_search, "build_embedding_provider", lambda settings: fake_embedding_provider
    )
    # Seeds Postgres directly with a real commit (through a plain,
    # non-transactional session against the shared findr_test database) so
    # the script's own, separately-constructed engine can see it — the
    # transactional db_session fixture wouldn't be visible outside its own
    # connection.
    db = sessionmaker(bind=test_engine)()
    try:
        user = UserRepositoryPostgres(db).create("a@example.com", "hash")
        db.commit()
        connection = SourceConnectionRepositoryPostgres(db).create(
            user.id, ensure_workspace(db, user.id).id, SourceType.GMAIL, "a@gmail.com"
        )
        db.commit()
        DocumentRepositoryPostgres(db).upsert_many(
            [
                Document(
                    id=0,
                    user_id=user.id,
                    connection_id=connection.id,
                    external_id="msg-1",
                    subject="Q3 renewal terms",
                    sender="x@y.com",
                    recipients="Inbox",
                    body_text="please review the renewal terms",
                    sent_at=None,
                )
            ]
        )
        db.commit()
    finally:
        db.close()

    reindex_search.main()

    search_index = ElasticsearchIndex(es_client, es_index, fake_embedding_provider)
    hits = SearchDocuments(search_index).execute(user.id, connection.workspace_id, "renewal")
    assert len(hits) == 1
    assert hits[0].document.subject == "Q3 renewal terms"
    assert hits[0].source_type == SourceType.GMAIL
    assert hits[0].external_account == "a@gmail.com"
    # Gmail stays keyword-only: no chunk vectors (semantic-search.md §12).
    stored = es_client.get(index=es_index, id=str(hits[0].document.id))
    assert "chunks" not in stored["_source"]


def test_reindex_search_rebuilds_upload_chunk_vectors_from_postgres(
    app_env, monkeypatch, test_engine, es_client, es_index, fake_embedding_provider, chunk_factory
):
    monkeypatch.setattr(
        reindex_search, "build_embedding_provider", lambda settings: fake_embedding_provider
    )
    db = sessionmaker(bind=test_engine)()
    try:
        user = UserRepositoryPostgres(db).create("a@example.com", "hash")
        connection = SourceConnectionRepositoryPostgres(db).create(user.id, ensure_workspace(db, user.id).id, SourceType.FILE, None)
        [document] = DocumentRepositoryPostgres(db).upsert_many(
            [
                Document(
                    id=0,
                    user_id=user.id,
                    connection_id=connection.id,
                    external_id="upload-1",
                    subject="handbook.md",
                    sender=None,
                    recipients=None,
                    body_text="intro",
                    sent_at=None,
                    chunks=[chunk_factory("intro", 0), chunk_factory("zebra migration", 1)],
                )
            ]
        )
        db.commit()
    finally:
        db.close()

    reindex_search.main()

    stored = es_client.get(index=es_index, id=str(document.id))["_source"]["chunks"]
    assert [c["text"] for c in stored] == ["intro", "zebra migration"]
    assert all(len(c["embedding"]) == 1024 for c in stored)
    # No file was re-parsed: the chunks came from document_chunks, and the
    # vectors make the second chunk semantically findable.
    search_index = ElasticsearchIndex(es_client, es_index, fake_embedding_provider)
    hits = SearchDocuments(search_index).execute(user.id, connection.workspace_id, "zebra migration")
    assert [h.document.external_id for h in hits] == ["upload-1"]
