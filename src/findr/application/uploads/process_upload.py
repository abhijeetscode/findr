import logging
import time
import uuid
from datetime import datetime, timedelta
from enum import Enum

from findr.application.uploads.upload_file import UPLOADS_DISPLAY_NAME
from findr.domain.entities import Document, UploadedFile
from findr.domain.exceptions import ExtractionFailed, UnsupportedFileType
from findr.domain.value_objects import ChunkKind, SourceType, UploadStatus
from findr.observability import bind, log_event
from findr.observability.events import elapsed_ms
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
        start = time.perf_counter()
        upload = self._uploads.claim(upload_id, self._clock.now() - STALE_PROCESSING_AFTER)
        self._uow.commit()
        if upload is None:
            log_event(
                logger,
                "upload.processed",
                level=logging.DEBUG,
                upload_id=upload_id,
                outcome=Outcome.SKIPPED.value,
                duration_ms=elapsed_ms(start),
            )
            return Outcome.SKIPPED

        with bind(upload_id=upload.id, user_id=upload.user_id, workspace_id=upload.workspace_id):
            event: dict = {"attempt": upload.attempts + 1}
            log_event(
                logger,
                "upload.processing.started",
                queued_ms=_ms_between(upload.created_at, self._clock.now()),
                **event,
            )
            outcome = self._process(upload, event)
            log_event(
                logger,
                "upload.processed",
                f"Upload {upload.id} processed: {outcome.value}",
                level=logging.WARNING if outcome == Outcome.FAILED else logging.INFO,
                outcome=outcome.value,
                duration_ms=elapsed_ms(start),
                **event,
            )
            return outcome

    def _process(self, upload: UploadedFile, event: dict) -> Outcome:
        """Fills `event` with counts and timings for upload.processed."""
        upload_id = upload.id
        step = time.perf_counter()
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
            event.update(parse_ms=elapsed_ms(step), error_kind=type(exc).__name__)
            self._uploads.mark_failed(upload_id, str(exc))
            self._uow.commit()
            return Outcome.FAILED
        except Exception as exc:  # noqa: BLE001 - infrastructure trouble; retry
            event.update(parse_ms=elapsed_ms(step), error_kind=type(exc).__name__)
            logger.exception("Reading/parsing upload %s failed", upload_id)
            return self._retry(upload_id)
        event.update(
            parse_ms=elapsed_ms(step),
            chunks=len(parsed.chunks),
            table_chunks=sum(1 for c in parsed.chunks if c.kind == ChunkKind.TABLE),
        )

        step = time.perf_counter()

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
            event.update(index_ms=elapsed_ms(step), document_id=document.id)
            return Outcome.READY
        except Exception as exc:  # noqa: BLE001 - infrastructure trouble; retry
            event.update(index_ms=elapsed_ms(step), error_kind=type(exc).__name__)
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


def _ms_between(earlier: datetime, later: datetime) -> int | None:
    try:
        return round((later - earlier).total_seconds() * 1000)
    except TypeError:  # naive vs aware: not worth failing an upload over
        return None
