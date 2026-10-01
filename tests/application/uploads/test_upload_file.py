import hashlib
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from findr.application.uploads.delete_document import DeleteDocument
from findr.application.uploads.delete_upload import DeleteUpload
from findr.application.uploads.process_upload import (
    GAVE_UP_MESSAGE,
    MAX_ATTEMPTS,
    Outcome,
    ProcessUpload,
)
from findr.application.uploads.requeue_stale_uploads import RequeueStaleUploads
from findr.application.uploads.upload_file import UploadFile
from findr.domain.entities import (
    ChunkMetadata,
    Document,
    DocumentChunk,
    ParsedDocument,
    SourceConnection,
    UploadedFile,
)
from findr.domain.exceptions import (
    DocumentNotFound,
    ExtractionFailed,
    UnsupportedFileType,
    UploadNotFound,
)
from findr.domain.value_objects import ChunkKind, ConnectionStatus, SourceType, UploadStatus

SUPPORTED = frozenset({"text/plain", "application/pdf"})
NOW = datetime(2026, 9, 30, 12, 0)


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


class FakeUnitOfWork:
    def __init__(self, on_commit=None) -> None:
        self.commits = 0
        self.rollbacks = 0
        self._on_commit = on_commit

    def commit(self) -> None:
        self.commits += 1
        if self._on_commit:
            self._on_commit()

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeUploadQueue:
    def __init__(self, error: Exception | None = None, on_enqueue=None) -> None:
        self.enqueued: list[int] = []
        self._error = error
        self._on_enqueue = on_enqueue

    def enqueue(self, upload_id: int) -> None:
        if self._on_enqueue:
            self._on_enqueue(upload_id)
        if self._error:
            raise self._error
        self.enqueued.append(upload_id)


class FakeFileStorage:
    def __init__(self, read_error: Exception | None = None) -> None:
        self.files: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self._read_error = read_error

    def save(self, user_id, filename, content) -> str:
        path = f"{user_id}/file-{len(self.files)}"
        self.files[path] = content
        return path

    def read(self, storage_path) -> bytes:
        if self._read_error:
            raise self._read_error
        return self.files[storage_path]

    def delete(self, storage_path) -> None:
        self.deleted.append(storage_path)
        self.files.pop(storage_path, None)


class FakeUploadedFileRepository:
    """In-memory, with the real claim rules."""

    def __init__(self, clock: FixedClock) -> None:
        self.rows: dict[int, UploadedFile] = {}
        self._clock = clock

    def create(self, user_id, workspace_id, original_filename, mime_type, file_size_bytes,
               storage_path, content_sha256, document_version):
        now = self._clock.now()
        row = UploadedFile(
            id=len(self.rows) + 1, user_id=user_id, workspace_id=workspace_id,
            original_filename=original_filename,
            mime_type=mime_type, file_size_bytes=file_size_bytes, storage_path=storage_path,
            content_sha256=content_sha256, document_version=document_version,
            status=UploadStatus.PENDING, document_id=None, error=None, attempts=0,
            created_at=now, updated_at=now,
        )
        self.rows[row.id] = row
        return row

    def get(self, upload_id, user_id):
        row = self.rows.get(upload_id)
        return row if row is not None and row.user_id == user_id else None

    def get_by_document_id(self, document_id):
        return next((r for r in self.rows.values() if r.document_id == document_id), None)

    def list_for_workspace(self, workspace_id):
        return [r for r in self.rows.values() if r.workspace_id == workspace_id]

    def claim(self, upload_id, stale_before):
        row = self.rows.get(upload_id)
        if row is None:
            return None
        claimable = row.status == UploadStatus.PENDING or (
            row.status == UploadStatus.PROCESSING and row.updated_at < stale_before
        )
        if not claimable:
            return None
        return self._update(upload_id, status=UploadStatus.PROCESSING)

    def lock(self, upload_id):
        return self.rows.get(upload_id)

    def mark_ready(self, upload_id, document_id):
        self._update(upload_id, status=UploadStatus.READY, document_id=document_id, error=None)

    def mark_failed(self, upload_id, error):
        self._update(upload_id, status=UploadStatus.FAILED, error=error)

    def mark_retry(self, upload_id):
        row = self.rows[upload_id]
        return self._update(upload_id, status=UploadStatus.PENDING, attempts=row.attempts + 1).attempts

    def list_stale(self, pending_before, processing_before):
        return [
            r for r in self.rows.values()
            if (r.status == UploadStatus.PENDING and r.updated_at < pending_before)
            or (r.status == UploadStatus.PROCESSING and r.updated_at < processing_before)
        ]

    def delete(self, upload_id):
        self.rows.pop(upload_id, None)

    def _update(self, upload_id, **values):
        row = replace(self.rows[upload_id], updated_at=self._clock.now(), **values)
        self.rows[upload_id] = row
        return row


class FakeDocumentParser:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[tuple] = []

    def parse(self, content, mime_type, filename, document_version, workspace_id):
        self.calls.append((content, mime_type, filename, document_version, workspace_id))
        if self._error:
            raise self._error
        chunk = DocumentChunk(
            text="quarterly renewal terms",
            kind=ChunkKind.TEXT,
            table_html=None,
            metadata=ChunkMetadata(
                chunk_index=0, document_version=document_version,
                content_sha256=hashlib.sha256(content).hexdigest(), workspace_id=workspace_id,
                filename=filename,
                mime_type=mime_type, page_start=None, page_end=None, section_title=None,
                element_types=["NarrativeText"], languages=["eng"], is_continuation=False,
                parser_version="test",
            ),
        )
        return ParsedDocument(text="quarterly renewal terms", chunks=[chunk])


class FakeSourceConnectionRepository:
    def __init__(self) -> None:
        self.connections: list[SourceConnection] = []

    def create(self, user_id, workspace_id, source_type, external_account, display_name=None):
        connection = SourceConnection(
            id=len(self.connections) + 1, user_id=user_id, workspace_id=workspace_id,
            source_type=source_type,
            external_account=external_account, status=ConnectionStatus.ACTIVE, sync_cursor=None,
            last_synced_at=None, last_error=None, created_at=NOW, display_name=display_name,
        )
        self.connections.append(connection)
        return connection

    def get_in_workspace(self, workspace_id, source_type, external_account):
        return next(
            (c for c in self.connections
             if (c.workspace_id, c.source_type, c.external_account)
             == (workspace_id, source_type, external_account)),
            None,
        )


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

    def delete_by_id(self, document_id):
        self.documents.pop(document_id, None)


class FakeSearchIndex:
    def __init__(self, index_error: Exception | None = None) -> None:
        self.indexed: list[tuple[list[Document], SourceType, str | None]] = []
        self.deleted: list[tuple[int, list[str]]] = []
        self._index_error = index_error

    def index_documents(self, documents, source_type, external_account, workspace_id):
        self.indexed.append((list(documents), source_type, external_account, workspace_id))
        if self._index_error:
            raise self._index_error

    def delete_documents(self, connection_id, external_ids):
        self.deleted.append((connection_id, external_ids))


class Env:
    def __init__(self, *, parser=None, storage=None, search_index=None, queue=None) -> None:
        self.clock = FixedClock()
        self.uploads = FakeUploadedFileRepository(self.clock)
        self.storage = storage or FakeFileStorage()
        self.parser = parser or FakeDocumentParser()
        self.connections = FakeSourceConnectionRepository()
        self.documents = FakeDocumentRepository()
        self.search_index = search_index or FakeSearchIndex()
        self.uow = FakeUnitOfWork()
        self.queue = queue or FakeUploadQueue()

    def upload(self, user_id=7, filename="terms.txt", mime="text/plain", content=b"hello",
               workspace_id=1):
        use_case = UploadFile(SUPPORTED, self.storage, self.uploads, self.uow, self.queue)
        return use_case.execute(user_id, workspace_id, filename, mime, content)

    def process(self, upload_id):
        return ProcessUpload(
            self.uploads, self.storage, self.parser, self.connections, self.documents,
            self.search_index, self.uow, self.clock,
        ).execute(upload_id)

    def delete_upload(self, upload_id, user_id=7):
        DeleteUpload(self.uploads, self.documents, self.search_index, self.storage).execute(
            upload_id, user_id
        )

    def delete_document(self, document_id, user_id=7):
        DeleteDocument(self.documents, self.uploads, self.search_index, self.storage).execute(
            document_id, user_id
        )

    def sweep(self):
        return RequeueStaleUploads(self.uploads, self.uow, self.queue, self.clock).execute()


# ---------- UploadFile (request side) ----------


def test_upload_stores_records_pending_and_enqueues_after_commit():
    commits_at_enqueue = []
    env = Env()
    env.queue = FakeUploadQueue(on_enqueue=lambda _id: commits_at_enqueue.append(env.uow.commits))

    upload = env.upload(content=b"hello world")

    assert upload.status == UploadStatus.PENDING
    assert upload.document_id is None
    assert upload.document_version == 1
    assert upload.workspace_id == 1
    assert upload.content_sha256 == hashlib.sha256(b"hello world").hexdigest()
    assert upload.file_size_bytes == 11
    assert env.storage.files[upload.storage_path] == b"hello world"
    assert env.queue.enqueued == [upload.id]
    # The row was committed before the worker could be told about it.
    assert commits_at_enqueue == [1]


@pytest.mark.parametrize(
    ("mime", "content", "error"),
    [("image/png", b"\x89PNG", UnsupportedFileType), ("text/plain", b"", ExtractionFailed)],
)
def test_upload_rejects_unsupported_or_empty_files_without_storing(mime, content, error):
    env = Env()
    with pytest.raises(error):
        env.upload(mime=mime, content=content)
    assert env.storage.files == {}
    assert env.uploads.rows == {}
    assert env.queue.enqueued == []


def test_upload_succeeds_even_if_enqueue_fails():
    # The row stays PENDING; the stale-upload sweep re-enqueues it later.
    env = Env(queue=FakeUploadQueue(error=ConnectionError("redis down")))

    upload = env.upload()

    assert env.uploads.rows[upload.id].status == UploadStatus.PENDING


# ---------- ProcessUpload (worker side) ----------


def test_process_creates_document_with_chunks_indexes_and_marks_ready():
    env = Env()
    upload = env.upload(filename="terms.txt", content=b"hello")

    assert env.process(upload.id) == Outcome.READY

    row = env.uploads.rows[upload.id]
    assert row.status == UploadStatus.READY
    document = env.documents.documents[row.document_id]
    assert document.subject == "terms.txt"
    assert document.body_text == "quarterly renewal terms"
    assert [c.text for c in document.chunks] == ["quarterly renewal terms"]
    assert env.parser.calls == [(b"hello", "text/plain", "terms.txt", 1, 1)]
    [connection] = env.connections.connections
    assert (connection.source_type, connection.external_account) == (SourceType.FILE, None)
    assert connection.display_name == "Uploaded files"
    assert env.search_index.indexed == [([document], SourceType.FILE, None, 1)]
    assert connection.workspace_id == 1
    assert document.chunks[0].metadata.workspace_id == 1


def test_each_workspace_gets_its_own_uploads_connection():
    env = Env()
    a = env.upload(content=b"a", workspace_id=1)
    b = env.upload(content=b"b", workspace_id=2)
    env.process(a.id)
    env.process(b.id)

    by_workspace = {c.workspace_id: c for c in env.connections.connections}
    assert set(by_workspace) == {1, 2}
    indexed_workspaces = [entry[3] for entry in env.search_index.indexed]
    assert indexed_workspaces == [1, 2]


def test_second_upload_reuses_the_uploads_connection():
    env = Env()
    a = env.upload(content=b"a")
    b = env.upload(content=b"b")
    env.process(a.id)
    env.process(b.id)

    assert len(env.connections.connections) == 1
    docs = list(env.documents.documents.values())
    assert docs[0].external_id != docs[1].external_id


@pytest.mark.parametrize("error", [ExtractionFailed("No text"), UnsupportedFileType("png")])
def test_parse_failure_fails_immediately_without_retry(error):
    env = Env(parser=FakeDocumentParser(error=error))
    upload = env.upload()

    assert env.process(upload.id) == Outcome.FAILED

    row = env.uploads.rows[upload.id]
    assert row.status == UploadStatus.FAILED
    assert row.error == str(error)
    assert row.attempts == 0
    assert env.documents.documents == {}


def test_infrastructure_error_retries_then_gives_up():
    env = Env(search_index=FakeSearchIndex(index_error=ConnectionError("es down")))
    upload = env.upload()

    for attempt in range(1, MAX_ATTEMPTS):
        assert env.process(upload.id) == Outcome.RETRY
        assert env.uploads.rows[upload.id].status == UploadStatus.PENDING
        assert env.uploads.rows[upload.id].attempts == attempt
    assert env.process(upload.id) == Outcome.FAILED

    row = env.uploads.rows[upload.id]
    assert row.status == UploadStatus.FAILED
    assert row.error == GAVE_UP_MESSAGE
    # Each failed attempt rolled back and removed its half-indexed entry.
    assert env.uow.rollbacks == MAX_ATTEMPTS
    assert len(env.search_index.deleted) == MAX_ATTEMPTS


def test_storage_read_error_is_retried_not_failed():
    env = Env(storage=FakeFileStorage(read_error=OSError("disk gone")))
    env.storage.files = {}
    upload = env.upload()

    assert env.process(upload.id) == Outcome.RETRY
    assert env.uploads.rows[upload.id].status == UploadStatus.PENDING


def test_duplicate_or_finished_messages_are_skipped():
    env = Env()
    upload = env.upload()
    assert env.process(upload.id) == Outcome.READY

    assert env.process(upload.id) == Outcome.SKIPPED
    assert env.process(999) == Outcome.SKIPPED
    assert len(env.documents.documents) == 1


def test_processing_upload_is_not_claimed_twice_until_stale():
    env = Env()
    upload = env.upload()
    env.uploads.claim(upload.id, stale_before=NOW)  # another worker holds it

    assert env.process(upload.id) == Outcome.SKIPPED

    env.clock.current = NOW + timedelta(minutes=31)  # that worker died
    assert env.process(upload.id) == Outcome.READY


def test_upload_deleted_while_parsing_leaves_nothing_behind():
    env = Env()
    upload = env.upload()

    class DeletingParser(FakeDocumentParser):
        def parse(self, *args):
            parsed = super().parse(*args)
            env.uploads.delete(upload.id)  # DELETE /uploads/{id} meanwhile
            return parsed

    env.parser = DeletingParser()
    assert env.process(upload.id) == Outcome.SKIPPED
    assert env.documents.documents == {}
    assert env.search_index.indexed == []


# ---------- deletes ----------


def test_delete_upload_in_every_status():
    env = Env()
    pending = env.upload(content=b"p")
    ready = env.upload(content=b"r")
    env.process(ready.id)
    document_id = env.uploads.rows[ready.id].document_id
    document = env.documents.documents[document_id]

    env.delete_upload(pending.id)
    env.delete_upload(ready.id)

    assert env.uploads.rows == {}
    assert env.documents.documents == {}
    assert env.search_index.deleted == [(document.connection_id, [document.external_id])]
    assert sorted(env.storage.deleted) == sorted([pending.storage_path, ready.storage_path])


def test_delete_upload_of_another_user_is_not_found():
    env = Env()
    upload = env.upload(user_id=7)
    with pytest.raises(UploadNotFound):
        env.delete_upload(upload.id, user_id=8)
    assert upload.id in env.uploads.rows


def test_delete_document_also_removes_its_upload_record_and_file():
    env = Env()
    upload = env.upload()
    env.process(upload.id)
    document_id = env.uploads.rows[upload.id].document_id

    env.delete_document(document_id)

    assert env.uploads.rows == {}
    assert env.documents.documents == {}
    assert env.storage.deleted == [upload.storage_path]
    with pytest.raises(DocumentNotFound):
        env.delete_document(document_id)


# ---------- stale-upload sweep ----------


def test_sweep_requeues_lost_pending_and_dead_processing_uploads():
    env = Env()
    lost = env.upload(content=b"lost")
    stuck = env.upload(content=b"stuck")
    fresh = env.upload(content=b"fresh")
    env.uploads.claim(stuck.id, stale_before=NOW)
    env.queue.enqueued.clear()

    env.clock.current = NOW + timedelta(minutes=31)
    env.uploads.rows[fresh.id] = replace(env.uploads.rows[fresh.id], updated_at=env.clock.current)

    requeued = env.sweep()

    assert sorted(requeued) == sorted([lost.id, stuck.id])
    assert sorted(env.queue.enqueued) == sorted([lost.id, stuck.id])
    # A dead worker counts as an attempt.
    assert env.uploads.rows[stuck.id].status == UploadStatus.PENDING
    assert env.uploads.rows[stuck.id].attempts == 1
    assert env.uploads.rows[lost.id].attempts == 0


def test_sweep_fails_an_upload_that_keeps_killing_the_worker():
    env = Env()
    upload = env.upload()
    env.uploads.rows[upload.id] = replace(
        env.uploads.rows[upload.id], status=UploadStatus.PROCESSING, attempts=MAX_ATTEMPTS - 1
    )
    env.clock.current = NOW + timedelta(minutes=31)
    env.queue.enqueued.clear()

    assert env.sweep() == []
    assert env.uploads.rows[upload.id].status == UploadStatus.FAILED
    assert env.queue.enqueued == []
