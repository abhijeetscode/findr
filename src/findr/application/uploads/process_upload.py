import logging
import uuid
from datetime import timedelta
from enum import Enum

from findr.application.uploads.upload_file import UPLOADS_DISPLAY_NAME
from findr.domain.entities import Document
from findr.domain.exceptions import ExtractionFailed, UnsupportedFileType
from findr.domain.value_objects import SourceType, UploadStatus
from findr.ports.clock import Clock
from findr.ports.document_parser import DocumentParser
from findr.ports.document_repository import DocumentRepository
from findr.ports.file_storage import FileStorage
from findr.ports.search_index import SearchIndex
from findr.ports.source_connection_repo import SourceConnectionRepository
from findr.ports.unit_of_work import UnitOfWork
from findr.ports.uploaded_file_repository import UploadedFileRepository

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
# A PROCESSING row untouched for this long belongs to a worker that died
# (a long OCR run on CPU stays well under it). Also the broker's redelivery
# timeout — see specs/upload-chunking.md §5.2/§7.
STALE_PROCESSING_AFTER = timedelta(minutes=30)
GAVE_UP_MESSAGE = "Processing failed after several attempts; try uploading the file again"


class Outcome(str, Enum):
    READY = "ready"
    FAILED = "failed"
    # Infrastructure error; back to PENDING — the caller should re-enqueue.
    RETRY = "retry"
    # Not claimable: a duplicate message, already processed, or deleted.
    SKIPPED = "skipped"


class ProcessUpload:
    """The worker half of an upload: claim → read → parse (OCR, tables,
    chunks) → Document + chunks in Postgres → embed + index → READY. See
    specs/upload-chunking.md §5.3.

    Parse failures are deterministic, so they fail the upload immediately.
    Anything else (Postgres/Elasticsearch/storage trouble) puts it back to
    PENDING for a retry, up to MAX_ATTEMPTS.
    """

    def __init__(
        self,
        uploaded_file_repo: UploadedFileRepository,
        file_storage: FileStorage,
        document_parser: DocumentParser,
        connection_repo: SourceConnectionRepository,
        document_repo: DocumentRepository,
        search_index: SearchIndex,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._uploads = uploaded_file_repo
        self._storage = file_storage
        self._parser = document_parser
        self._connections = connection_repo
        self._documents = document_repo
        self._search_index = search_index
        self._uow = unit_of_work
        self._clock = clock

    def execute(self, upload_id: int) -> Outcome:
        upload = self._uploads.claim(upload_id, self._clock.now() - STALE_PROCESSING_AFTER)
        self._uow.commit()
        if upload is None:
            return Outcome.SKIPPED

        try:
            content = self._storage.read(upload.storage_path)
            parsed = self._parser.parse(
                content,
                upload.mime_type,
                upload.original_filename,
                upload.document_version,
                upload.workspace_id,
            )
        except (ExtractionFailed, UnsupportedFileType) as exc:
            self._uploads.mark_failed(upload_id, str(exc))
            self._uow.commit()
            return Outcome.FAILED
        except Exception:  # noqa: BLE001 - infrastructure trouble; retry
            logger.exception("Reading/parsing upload %s failed", upload_id)
            return self._retry(upload_id)

        document: Document | None = None
        try:
            # Row-locked until commit: a concurrent DELETE /uploads/{id}
            # either finishes first (we see None and stop) or waits for us
            # (and then deletes the finished document too).
            locked = self._uploads.lock(upload_id)
            if locked is None or locked.status != UploadStatus.PROCESSING:
                self._uow.rollback()
                return Outcome.SKIPPED

            # Each workspace has its own "Uploaded files" connection.
            connection = self._connections.get_in_workspace(
                upload.workspace_id, SourceType.FILE, None
            )
            if connection is None:
                connection = self._connections.create(
                    upload.user_id,
                    upload.workspace_id,
                    SourceType.FILE,
                    None,
                    display_name=UPLOADS_DISPLAY_NAME,
                )
            [document] = self._documents.upsert_many(
                [
                    Document(
                        id=0,
                        user_id=upload.user_id,
                        connection_id=connection.id,
                        # Uploads have no natural external id; a fresh one
                        # per upload means same-named files never collide.
                        external_id=str(uuid.uuid4()),
                        subject=upload.original_filename,
                        sender=None,
                        recipients=None,
                        body_text=parsed.text,
                        sent_at=None,
                        chunks=parsed.chunks,
                    )
                ]
            )
            self._search_index.index_documents(
                [document], SourceType.FILE, None, upload.workspace_id
            )
            self._uploads.mark_ready(upload_id, document.id)
            self._uow.commit()
            return Outcome.READY
        except Exception:  # noqa: BLE001 - infrastructure trouble; retry
            logger.exception("Indexing upload %s failed", upload_id)
            self._uow.rollback()
            if document is not None:
                # The Postgres rows were rolled back; don't leave a searchable
                # index entry pointing at them.
                self._remove_from_index(document)
            return self._retry(upload_id)

    def _retry(self, upload_id: int) -> Outcome:
        attempts = self._uploads.mark_retry(upload_id)
        if attempts >= MAX_ATTEMPTS:
            self._uploads.mark_failed(upload_id, GAVE_UP_MESSAGE)
            self._uow.commit()
            return Outcome.FAILED
        self._uow.commit()
        return Outcome.RETRY

    def _remove_from_index(self, document: Document) -> None:
        try:
            self._search_index.delete_documents(document.connection_id, [document.external_id])
        except Exception:  # noqa: BLE001 - best effort
            logger.exception("Could not remove half-indexed document %s", document.id)
