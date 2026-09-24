from datetime import datetime

from findr.adapters.outbound.sqlite.document_repository_sqlite import DocumentRepositorySqlite
from findr.adapters.outbound.sqlite.search_index_sqlite import SearchIndexSqlite
from findr.adapters.outbound.sqlite.source_connection_repo_sqlite import (
    SourceConnectionRepositorySqlite,
)
from findr.adapters.outbound.sqlite.user_repository_sqlite import UserRepositorySqlite
from findr.application.search.search_documents import SearchDocuments
from findr.domain.entities import Document
from findr.domain.value_objects import SourceType


def _make_connection(db_session, user_id: int, email: str) -> int:
    connection = SourceConnectionRepositorySqlite(db_session).create(
        user_id, SourceType.GMAIL, email
    )
    db_session.commit()
    return connection.id


def _make_document(*, user_id, connection_id, external_id, subject, body_text) -> Document:
    return Document(
        id=0,  # ignored on insert; SQLite assigns the real id
        user_id=user_id,
        connection_id=connection_id,
        external_id=external_id,
        subject=subject,
        sender="someone@example.com",
        recipients=None,
        body_text=body_text,
        sent_at=datetime(2024, 1, 1),
    )


def test_search_is_isolated_per_user(db_session):
    user_repo = UserRepositorySqlite(db_session)
    user_a = user_repo.create("a@example.com", "hash")
    user_b = user_repo.create("b@example.com", "hash")
    db_session.commit()
    connection_a = _make_connection(db_session, user_a.id, "a@gmail.com")
    connection_b = _make_connection(db_session, user_b.id, "b@gmail.com")

    doc_repo = DocumentRepositorySqlite(db_session)
    doc_repo.upsert_many(
        [
            _make_document(
                user_id=user_a.id,
                connection_id=connection_a,
                external_id="msg-1",
                subject="Renewal terms",
                body_text="Please review the renewal terms.",
            ),
            _make_document(
                user_id=user_b.id,
                connection_id=connection_b,
                external_id="msg-1",
                subject="Renewal terms",
                body_text="Please review the renewal terms.",
            ),
        ]
    )
    db_session.commit()

    use_case = SearchDocuments(SearchIndexSqlite(db_session))

    a_hits = use_case.execute(user_a.id, "renewal")
    b_hits = use_case.execute(user_b.id, "renewal")

    assert len(a_hits) == 1
    assert a_hits[0].document.user_id == user_a.id
    assert len(b_hits) == 1
    assert b_hits[0].document.user_id == user_b.id


def test_upsert_dedups_on_connection_and_external_id(db_session):
    user = UserRepositorySqlite(db_session).create("a@example.com", "hash")
    db_session.commit()
    connection_id = _make_connection(db_session, user.id, "a@gmail.com")
    doc_repo = DocumentRepositorySqlite(db_session)

    doc_repo.upsert_many(
        [
            _make_document(
                user_id=user.id,
                connection_id=connection_id,
                external_id="msg-1",
                subject="Old subject",
                body_text="old body",
            )
        ]
    )
    db_session.commit()
    doc_repo.upsert_many(
        [
            _make_document(
                user_id=user.id,
                connection_id=connection_id,
                external_id="msg-1",
                subject="New subject",
                body_text="new body",
            )
        ]
    )
    db_session.commit()

    search = SearchDocuments(SearchIndexSqlite(db_session))
    new_hits = search.execute(user.id, "new")
    assert len(new_hits) == 1
    assert new_hits[0].document.subject == "New subject"

    assert search.execute(user.id, "old") == []


def test_delete_many_removes_from_search(db_session):
    user = UserRepositorySqlite(db_session).create("a@example.com", "hash")
    db_session.commit()
    connection_id = _make_connection(db_session, user.id, "a@gmail.com")
    doc_repo = DocumentRepositorySqlite(db_session)
    doc_repo.upsert_many(
        [
            _make_document(
                user_id=user.id,
                connection_id=connection_id,
                external_id="msg-1",
                subject="Trashed",
                body_text="this will be deleted",
            )
        ]
    )
    db_session.commit()

    search = SearchDocuments(SearchIndexSqlite(db_session))
    assert len(search.execute(user.id, "trashed")) == 1

    doc_repo.delete_many(connection_id=connection_id, external_ids=["msg-1"])
    db_session.commit()

    assert search.execute(user.id, "trashed") == []


def test_query_with_fts5_special_characters_does_not_error(db_session):
    user = UserRepositorySqlite(db_session).create("a@example.com", "hash")
    db_session.commit()
    connection_id = _make_connection(db_session, user.id, "a@gmail.com")
    doc_repo = DocumentRepositorySqlite(db_session)
    doc_repo.upsert_many(
        [
            _make_document(
                user_id=user.id,
                connection_id=connection_id,
                external_id="msg-1",
                subject="Q3 renewal",
                body_text="terms: 24 months",
            )
        ]
    )
    db_session.commit()

    search = SearchDocuments(SearchIndexSqlite(db_session))
    hits = search.execute(user.id, 'terms: "24" -months*')

    assert isinstance(hits, list)  # must not raise a FTS5 syntax error
