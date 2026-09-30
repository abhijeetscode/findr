from datetime import datetime

import pytest

from findr.application.uploads.delete_document import DeleteDocument
from findr.application.uploads.upload_file import UploadFile
from findr.domain.entities import Document, SourceConnection, UploadedFile
from findr.domain.exceptions import DocumentNotFound, ExtractionFailed, UnsupportedFileType
from findr.domain.value_objects import ConnectionStatus, SourceType


class FakeTextExtractor:
    def __init__(self, text: str = "extracted text", error: Exception | None = None) -> None:
        self._text = text
        self._error = error
        self.calls: list[tuple[bytes, str]] = []

    def extract(self, content, mime_type) -> str:
        self.calls.append((content, mime_type))
        if self._error is not None:
            raise self._error
        return self._text


class FakeFileStorage:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def save(self, user_id, filename, content) -> str:
        path = f"{user_id}/file-{len(self.files)}"
        self.files[path] = content
        return path

    def read(self, storage_path) -> bytes:
        return self.files[storage_path]

    def delete(self, storage_path) -> None:
        self.deleted.append(storage_path)
        self.files.pop(storage_path, None)


class FakeSourceConnectionRepository:
    def __init__(self) -> None:
        self.connections: list[SourceConnection] = []

    def create(self, user_id, source_type, external_account, display_name=None):
        connection = SourceConnection(
            id=len(self.connections) + 1,
            user_id=user_id,
            source_type=source_type,
            external_account=external_account,
            status=ConnectionStatus.ACTIVE,
            sync_cursor=None,
            last_synced_at=None,
            last_error=None,
            created_at=datetime(2024, 1, 1),
            display_name=display_name,
        )
        self.connections.append(connection)
        return connection

    def get_by_account(self, user_id, source_type, external_account):
        for c in self.connections:
            if (c.user_id, c.source_type, c.external_account) == (
                user_id,
                source_type,
                external_account,
            ):
                return c
        return None


class FakeDocumentRepository:
    def __init__(self) -> None:
        self.documents: dict[int, Document] = {}
        self._next_id = 100

    def upsert_many(self, documents):
        for doc in documents:
            doc.id = self._next_id
            self._next_id += 1
            self.documents[doc.id] = doc
        return documents

    def get(self, document_id, user_id):
        doc = self.documents.get(document_id)
        return doc if doc is not None and doc.user_id == user_id else None

    def delete_by_id(self, document_id) -> None:
        self.documents.pop(document_id, None)


class FakeUploadedFileRepository:
    def __init__(self, document_repo: FakeDocumentRepository | None = None) -> None:
        self.rows: dict[int, UploadedFile] = {}
        self._document_repo = document_repo

    def create(self, document_id, user_id, original_filename, mime_type, file_size_bytes, storage_path):
        row = UploadedFile(
            id=len(self.rows) + 1,
            document_id=document_id,
            user_id=user_id,
            original_filename=original_filename,
            mime_type=mime_type,
            file_size_bytes=file_size_bytes,
            storage_path=storage_path,
            created_at=datetime(2024, 1, 1),
        )
        self.rows[document_id] = row
        return row

    def get_by_document_id(self, document_id):
        return self.rows.get(document_id)

    def delete_by_document_id(self, document_id) -> None:
        # Mirrors the real FK: the child row must go before its document.
        if self._document_repo is not None:
            assert document_id in self._document_repo.documents
        self.rows.pop(document_id, None)


class FakeSearchIndex:
    def __init__(self) -> None:
        self.indexed: list[tuple[list[Document], SourceType, str | None]] = []
        self.deleted: list[tuple[int, list[str]]] = []

    def index_documents(self, documents, source_type, external_account) -> None:
        self.indexed.append((list(documents), source_type, external_account))

    def delete_documents(self, connection_id, external_ids) -> None:
        self.deleted.append((connection_id, external_ids))


class Env:
    def __init__(self, extractor: FakeTextExtractor | None = None) -> None:
        self.extractor = extractor or FakeTextExtractor()
        self.storage = FakeFileStorage()
        self.connections = FakeSourceConnectionRepository()
        self.documents = FakeDocumentRepository()
        self.uploads = FakeUploadedFileRepository(self.documents)
        self.search_index = FakeSearchIndex()

    def upload(self) -> UploadFile:
        return UploadFile(
            self.extractor,
            self.storage,
            self.connections,
            self.documents,
            self.uploads,
            self.search_index,
        )

    def delete(self) -> DeleteDocument:
        return DeleteDocument(self.documents, self.uploads, self.search_index, self.storage)


def test_upload_creates_connection_document_row_and_index_entry():
    env = Env(FakeTextExtractor("quarterly renewal terms"))

    document, uploaded = env.upload().execute(7, "terms.pdf", "application/pdf", b"%PDF")

    [connection] = env.connections.connections
    assert connection.source_type == SourceType.FILE
    assert connection.external_account is None
    assert connection.display_name == "Uploaded files"

    assert document.id == 100
    assert document.user_id == 7
    assert document.connection_id == connection.id
    assert document.subject == "terms.pdf"
    assert document.body_text == "quarterly renewal terms"
    assert document.sender is None and document.sent_at is None

    assert uploaded.document_id == document.id
    assert uploaded.original_filename == "terms.pdf"
    assert uploaded.mime_type == "application/pdf"
    assert uploaded.file_size_bytes == 4
    assert env.storage.files[uploaded.storage_path] == b"%PDF"

    assert env.search_index.indexed == [([document], SourceType.FILE, None)]
    assert env.extractor.calls == [(b"%PDF", "application/pdf")]


def test_second_upload_reuses_the_connection_and_gets_a_fresh_external_id():
    env = Env()
    first, _ = env.upload().execute(7, "notes.txt", "text/plain", b"a")
    second, _ = env.upload().execute(7, "notes.txt", "text/plain", b"b")

    assert len(env.connections.connections) == 1
    assert first.connection_id == second.connection_id
    assert first.external_id != second.external_id
    assert first.id != second.id


def test_each_user_gets_their_own_uploads_connection():
    env = Env()
    a, _ = env.upload().execute(1, "a.txt", "text/plain", b"a")
    b, _ = env.upload().execute(2, "b.txt", "text/plain", b"b")

    assert a.connection_id != b.connection_id


@pytest.mark.parametrize("error", [UnsupportedFileType("png"), ExtractionFailed("empty")])
def test_failed_extraction_writes_nothing(error):
    env = Env(FakeTextExtractor(error=error))

    with pytest.raises(type(error)):
        env.upload().execute(7, "x.png", "image/png", b"\x89PNG")

    assert env.storage.files == {}
    assert env.connections.connections == []
    assert env.documents.documents == {}
    assert env.search_index.indexed == []


def test_delete_removes_row_document_index_entry_and_file():
    env = Env()
    document, uploaded = env.upload().execute(7, "a.txt", "text/plain", b"a")

    env.delete().execute(document.id, 7)

    assert env.uploads.rows == {}
    assert env.documents.documents == {}
    assert env.search_index.deleted == [(document.connection_id, [document.external_id])]
    assert env.storage.deleted == [uploaded.storage_path]


def test_delete_of_another_users_document_is_not_found_and_changes_nothing():
    env = Env()
    document, _ = env.upload().execute(7, "a.txt", "text/plain", b"a")

    with pytest.raises(DocumentNotFound):
        env.delete().execute(document.id, 8)
    with pytest.raises(DocumentNotFound):
        env.delete().execute(999, 7)

    assert document.id in env.documents.documents
    assert env.search_index.deleted == []
    assert env.storage.deleted == []


def test_delete_of_a_non_upload_document_skips_file_storage():
    env = Env()
    [document] = env.documents.upsert_many(
        [
            Document(
                id=0,
                user_id=7,
                connection_id=3,
                external_id="msg-1",
                subject="s",
                sender=None,
                recipients=None,
                body_text="b",
                sent_at=None,
            )
        ]
    )

    env.delete().execute(document.id, 7)

    assert env.documents.documents == {}
    assert env.search_index.deleted == [(3, ["msg-1"])]
    assert env.storage.deleted == []
