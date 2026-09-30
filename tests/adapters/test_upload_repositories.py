from findr.adapters.outbound.postgres.document_repository_postgres import (
    DocumentRepositoryPostgres,
)
from findr.adapters.outbound.postgres.source_connection_repo_postgres import (
    SourceConnectionRepositoryPostgres,
)
from findr.adapters.outbound.postgres.uploaded_file_repository_postgres import (
    UploadedFileRepositoryPostgres,
)
from findr.adapters.outbound.postgres.user_repository_postgres import UserRepositoryPostgres
from findr.domain.entities import Document
from findr.domain.value_objects import SourceType


def _document(user_id: int, connection_id: int, external_id: str) -> Document:
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
    )


def test_get_by_account_matches_a_null_external_account(db_session):
    user = UserRepositoryPostgres(db_session).create("a@example.com", "hash")
    repo = SourceConnectionRepositoryPostgres(db_session)
    gmail = repo.create(user.id, SourceType.GMAIL, "a@gmail.com")

    assert repo.get_by_account(user.id, SourceType.FILE, None) is None

    uploads = repo.create(user.id, SourceType.FILE, None, display_name="Uploaded files")

    found = repo.get_by_account(user.id, SourceType.FILE, None)
    assert found is not None and found.id == uploads.id
    assert found.external_account is None
    assert repo.get_by_account(user.id, SourceType.GMAIL, "a@gmail.com").id == gmail.id


def test_document_get_is_user_scoped_and_delete_by_id(db_session):
    users = UserRepositoryPostgres(db_session)
    owner = users.create("a@example.com", "hash")
    other = users.create("b@example.com", "hash")
    connection = SourceConnectionRepositoryPostgres(db_session).create(
        owner.id, SourceType.FILE, None
    )
    repo = DocumentRepositoryPostgres(db_session)
    [document] = repo.upsert_many([_document(owner.id, connection.id, "uuid-1")])

    found = repo.get(document.id, owner.id)
    assert found is not None
    assert (found.external_id, found.subject, found.body_text) == ("uuid-1", "notes.txt", "hello")
    assert repo.get(document.id, other.id) is None
    assert repo.get(document.id + 1000, owner.id) is None

    repo.delete_by_id(document.id)
    assert repo.get(document.id, owner.id) is None


def test_uploaded_file_repository_round_trip(db_session):
    user = UserRepositoryPostgres(db_session).create("a@example.com", "hash")
    connection = SourceConnectionRepositoryPostgres(db_session).create(
        user.id, SourceType.FILE, None
    )
    [document] = DocumentRepositoryPostgres(db_session).upsert_many(
        [_document(user.id, connection.id, "uuid-1")]
    )
    repo = UploadedFileRepositoryPostgres(db_session)

    created = repo.create(
        document_id=document.id,
        user_id=user.id,
        original_filename="notes.txt",
        mime_type="text/plain",
        file_size_bytes=5,
        storage_path=f"{user.id}/abc.txt",
    )

    found = repo.get_by_document_id(document.id)
    assert found == created
    assert found.storage_path == f"{user.id}/abc.txt"

    repo.delete_by_document_id(document.id)
    assert repo.get_by_document_id(document.id) is None
    # With the child row gone, the document itself can be deleted (FK).
    DocumentRepositoryPostgres(db_session).delete_by_id(document.id)
