from datetime import datetime, timedelta

from sqlalchemy import func, select

from findr.adapters.outbound.postgres.document_repository_postgres import (
    DocumentRepositoryPostgres,
    load_chunks,
)
from findr.adapters.outbound.postgres.models import DocumentChunkModel
from findr.adapters.outbound.postgres.source_connection_repo_postgres import (
    SourceConnectionRepositoryPostgres,
)
from findr.adapters.outbound.postgres.uploaded_file_repository_postgres import (
    UploadedFileRepositoryPostgres,
)
from findr.adapters.outbound.postgres.user_repository_postgres import UserRepositoryPostgres
from findr.adapters.outbound.system_clock import SystemClock
from conftest import make_workspace
from findr.domain.entities import Document
from findr.domain.value_objects import ChunkKind, SourceType, UploadStatus


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


def _document(user_id: int, connection_id: int, external_id: str, chunks=()) -> Document:
    return Document(
        id=0,
        user_id=user_id,
        connection_id=connection_id,
        external_id=external_id,
        subject="notes.txt",
        sender=None,
        recipients=None,
        body_text="hello",
        sent_at=None,
        chunks=list(chunks),
    )


def _user_and_uploads_connection(db):
    user = UserRepositoryPostgres(db).create("a@example.com", "hash")
    workspace = make_workspace(db, user.id)
    connection = SourceConnectionRepositoryPostgres(db).create(
        user.id, workspace.id, SourceType.FILE, None
    )
    return user, connection


def _chunk_count(db) -> int:
    return db.execute(select(func.count()).select_from(DocumentChunkModel)).scalar_one()


def _create_upload(repo, workspace, name="notes.txt"):
    return repo.create(
        user_id=workspace.user_id,
        workspace_id=workspace.id,
        original_filename=name,
        mime_type="text/plain",
        file_size_bytes=5,
        storage_path=f"{workspace.user_id}/{name}",
        content_sha256="a" * 64,
        document_version=1,
    )


def test_get_by_account_matches_a_null_external_account(db_session):
    user = UserRepositoryPostgres(db_session).create("a@example.com", "hash")
    workspace = make_workspace(db_session, user.id)
    repo = SourceConnectionRepositoryPostgres(db_session)
    gmail = repo.create(user.id, workspace.id, SourceType.GMAIL, "a@gmail.com")

    assert repo.get_by_account(user.id, SourceType.FILE, None) is None

    uploads = repo.create(
        user.id, workspace.id, SourceType.FILE, None, display_name="Uploaded files"
    )

    found = repo.get_by_account(user.id, SourceType.FILE, None)
    assert found is not None and found.id == uploads.id
    assert found.workspace_id == workspace.id
    assert repo.get_by_account(user.id, SourceType.GMAIL, "a@gmail.com").id == gmail.id


def test_connections_are_found_and_listed_per_workspace(db_session):
    user = UserRepositoryPostgres(db_session).create("a@example.com", "hash")
    a = make_workspace(db_session, user.id, "Client A")
    b = make_workspace(db_session, user.id, "Client B")
    repo = SourceConnectionRepositoryPostgres(db_session)
    uploads_a = repo.create(user.id, a.id, SourceType.FILE, None, display_name="Uploaded files")
    uploads_b = repo.create(user.id, b.id, SourceType.FILE, None, display_name="Uploaded files")
    gmail_a = repo.create(user.id, a.id, SourceType.GMAIL, "a@gmail.com")

    # Each workspace has its own "Uploaded files" connection.
    assert repo.get_in_workspace(a.id, SourceType.FILE, None).id == uploads_a.id
    assert repo.get_in_workspace(b.id, SourceType.FILE, None).id == uploads_b.id
    assert repo.get_in_workspace(b.id, SourceType.GMAIL, "a@gmail.com") is None
    assert [c.id for c in repo.list_for_workspace(a.id)] == [uploads_a.id, gmail_a.id]
    assert [c.id for c in repo.list_for_workspace(b.id)] == [uploads_b.id]

    repo.delete(gmail_a.id)
    assert [c.id for c in repo.list_for_workspace(a.id)] == [uploads_a.id]


def test_chunks_round_trip_with_metadata_in_order(db_session, chunk_factory):
    user, connection = _user_and_uploads_connection(db_session)
    repo = DocumentRepositoryPostgres(db_session)
    chunks = [
        chunk_factory("intro text", 0, section_title="Intro", page_start=1, page_end=1),
        chunk_factory(
            "Plan 10 GBP",
            1,
            kind=ChunkKind.TABLE,
            table_html="<table><tr><td>Plan</td></tr></table>",
            element_types=["Table"],
            page_start=1,
            page_end=2,
            document_version=2,
            content_sha256="b" * 64,
            is_continuation=True,
        ),
    ]
    [document] = repo.upsert_many([_document(user.id, connection.id, "uuid-1", chunks)])

    loaded = repo.get(document.id, user.id).chunks

    assert loaded == chunks
    assert all(c.metadata.workspace_id == 1 for c in loaded)
    assert loaded[1].kind == ChunkKind.TABLE
    assert loaded[1].metadata.document_version == 2
    assert load_chunks(db_session, [document.id]) == {document.id: chunks}


def test_upsert_replaces_chunks_instead_of_appending(db_session, chunk_factory):
    user, connection = _user_and_uploads_connection(db_session)
    repo = DocumentRepositoryPostgres(db_session)
    repo.upsert_many([_document(user.id, connection.id, "uuid-1", [chunk_factory("old", 0), chunk_factory("old2", 1)])])
    [document] = repo.upsert_many([_document(user.id, connection.id, "uuid-1", [chunk_factory("new", 0)])])

    assert [c.text for c in repo.get(document.id, user.id).chunks] == ["new"]


def test_documents_without_chunks_never_touch_the_chunk_table(db_session, chunk_factory):
    user, connection = _user_and_uploads_connection(db_session)
    repo = DocumentRepositoryPostgres(db_session)
    [chunked] = repo.upsert_many([_document(user.id, connection.id, "uuid-1", [chunk_factory("x")])])

    # A Gmail-style re-upsert of the same document with no chunks leaves them.
    repo.upsert_many([_document(user.id, connection.id, "uuid-1")])

    assert _chunk_count(db_session) == 1


def test_deletes_remove_chunks_too(db_session, chunk_factory):
    user, connection = _user_and_uploads_connection(db_session)
    repo = DocumentRepositoryPostgres(db_session)
    [a, b] = repo.upsert_many(
        [
            _document(user.id, connection.id, "uuid-a", [chunk_factory("a")]),
            _document(user.id, connection.id, "uuid-b", [chunk_factory("b")]),
        ]
    )

    repo.delete_by_id(a.id)
    repo.delete_many(connection.id, ["uuid-b"])

    assert _chunk_count(db_session) == 0
    assert repo.get(a.id, user.id) is None and repo.get(b.id, user.id) is None


def test_document_get_is_user_scoped(db_session):
    users = UserRepositoryPostgres(db_session)
    owner = users.create("a@example.com", "hash")
    other = users.create("b@example.com", "hash")
    workspace = make_workspace(db_session, owner.id)
    connection = SourceConnectionRepositoryPostgres(db_session).create(
        owner.id, workspace.id, SourceType.FILE, None
    )
    repo = DocumentRepositoryPostgres(db_session)
    [document] = repo.upsert_many([_document(owner.id, connection.id, "uuid-1")])

    assert repo.get(document.id, owner.id).chunks == []
    assert repo.get(document.id, other.id) is None


def test_document_workspace_always_comes_from_its_connection(db_session):
    # Callers never decide it (specs/workspaces.md §3.2).
    user = UserRepositoryPostgres(db_session).create("a@example.com", "hash")
    a = make_workspace(db_session, user.id, "Client A")
    b = make_workspace(db_session, user.id, "Client B")
    connection = SourceConnectionRepositoryPostgres(db_session).create(
        user.id, a.id, SourceType.FILE, None
    )
    repo = DocumentRepositoryPostgres(db_session)
    lying = _document(user.id, connection.id, "uuid-1")
    lying.workspace_id = b.id

    [document] = repo.upsert_many([lying])

    assert document.workspace_id == a.id
    assert repo.get(document.id, user.id).workspace_id == a.id


def test_delete_for_workspace_removes_only_that_workspaces_documents(db_session, chunk_factory):
    user = UserRepositoryPostgres(db_session).create("a@example.com", "hash")
    a = make_workspace(db_session, user.id, "Client A")
    b = make_workspace(db_session, user.id, "Client B")
    connections = SourceConnectionRepositoryPostgres(db_session)
    conn_a = connections.create(user.id, a.id, SourceType.FILE, None)
    conn_b = connections.create(user.id, b.id, SourceType.FILE, None)
    repo = DocumentRepositoryPostgres(db_session)
    [doc_a] = repo.upsert_many([_document(user.id, conn_a.id, "a", [chunk_factory("a")])])
    [doc_b] = repo.upsert_many([_document(user.id, conn_b.id, "b", [chunk_factory("b")])])

    repo.delete_for_workspace(a.id)

    assert repo.get(doc_a.id, user.id) is None
    assert repo.get(doc_b.id, user.id) is not None
    assert _chunk_count(db_session) == 1


def test_upload_status_lifecycle(db_session):
    user, connection = _user_and_uploads_connection(db_session)
    clock = FixedClock(datetime(2026, 9, 30, 12, 0))
    repo = UploadedFileRepositoryPostgres(db_session, clock=clock)

    upload = _create_upload(repo, _workspace_of(db_session, connection, user))
    assert (upload.status, upload.document_id, upload.attempts) == (UploadStatus.PENDING, None, 0)
    assert upload.content_sha256 == "a" * 64 and upload.document_version == 1

    assert repo.claim(upload.id, stale_before=clock.now()).status == UploadStatus.PROCESSING
    assert repo.mark_retry(upload.id) == 1
    assert repo.get(upload.id, user.id).status == UploadStatus.PENDING

    repo.claim(upload.id, stale_before=clock.now())
    repo.mark_failed(upload.id, "No text")
    failed = repo.get(upload.id, user.id)
    assert (failed.status, failed.error) == (UploadStatus.FAILED, "No text")
    assert repo.claim(upload.id, stale_before=clock.now() + timedelta(days=1)) is None

    [document] = DocumentRepositoryPostgres(db_session).upsert_many(
        [_document(user.id, connection.id, "uuid-1")]
    )
    repo.mark_ready(upload.id, document.id)
    ready = repo.get_by_document_id(document.id)
    assert (ready.status, ready.error, ready.document_id) == (UploadStatus.READY, None, document.id)

    repo.delete(upload.id)
    assert repo.get(upload.id, user.id) is None


def test_list_and_get_are_user_scoped(db_session):
    users = UserRepositoryPostgres(db_session)
    a = users.create("a@example.com", "hash")
    b = users.create("b@example.com", "hash")
    repo = UploadedFileRepositoryPostgres(db_session)
    ws_a = make_workspace(db_session, a.id, "Client A")
    ws_a2 = make_workspace(db_session, a.id, "Client B")
    ws_b = make_workspace(db_session, b.id, "Client A")
    first = _create_upload(repo, ws_a, "one.txt")
    second = _create_upload(repo, ws_a, "two.txt")
    other_workspace = _create_upload(repo, ws_a2, "elsewhere.txt")
    _create_upload(repo, ws_b, "other.txt")

    assert [u.id for u in repo.list_for_workspace(ws_a.id)] == [second.id, first.id]
    assert [u.id for u in repo.list_for_workspace(ws_a2.id)] == [other_workspace.id]
    assert first.workspace_id == ws_a.id
    assert repo.get(first.id, b.id) is None


def test_claim_has_exactly_one_winner_across_sessions(db_session_factory):
    setup = db_session_factory()
    user = UserRepositoryPostgres(setup).create("a@example.com", "hash")
    upload = _create_upload(UploadedFileRepositoryPostgres(setup), make_workspace(setup, user.id))
    setup.commit()
    setup.close()

    # The repository stamps rows with SystemClock's naive UTC; compare in kind.
    now = SystemClock().now()
    first, second = db_session_factory(), db_session_factory()
    try:
        won = UploadedFileRepositoryPostgres(first).claim(upload.id, stale_before=now)
        first.commit()
        lost = UploadedFileRepositoryPostgres(second).claim(upload.id, stale_before=now)
        second.commit()
    finally:
        first.close()
        second.close()

    assert won is not None and won.status == UploadStatus.PROCESSING
    assert lost is None


def test_list_stale_finds_old_pending_and_processing(db_session):
    user = UserRepositoryPostgres(db_session).create("a@example.com", "hash")
    clock = FixedClock(datetime(2026, 9, 30, 12, 0))
    repo = UploadedFileRepositoryPostgres(db_session, clock=clock)
    workspace = make_workspace(db_session, user.id)
    pending = _create_upload(repo, workspace, "pending.txt")
    processing = _create_upload(repo, workspace, "processing.txt")
    repo.claim(processing.id, stale_before=clock.now())

    clock.current += timedelta(minutes=20)
    fresh = _create_upload(repo, workspace, "fresh.txt")

    stale = repo.list_stale(
        pending_before=clock.now() - timedelta(minutes=10),
        processing_before=clock.now() - timedelta(minutes=30),
    )

    assert [u.id for u in stale] == [pending.id]
    assert fresh.id not in [u.id for u in stale]


def _workspace_of(db, connection, user):
    from findr.adapters.outbound.postgres.workspace_repository_postgres import (
        WorkspaceRepositoryPostgres,
    )

    return WorkspaceRepositoryPostgres(db).get(connection.workspace_id, user.id)
