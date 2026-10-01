import logging
from datetime import timedelta

from findr.application.uploads.process_upload import (
    GAVE_UP_MESSAGE,
    MAX_ATTEMPTS,
    STALE_PROCESSING_AFTER,
)
from findr.domain.value_objects import UploadStatus
from findr.ports.clock import Clock
from findr.ports.unit_of_work import UnitOfWork
from findr.ports.upload_queue import UploadQueue
from findr.ports.uploaded_file_repository import UploadedFileRepository

logger = logging.getLogger(__name__)

# A PENDING upload should be picked up within seconds; this long means its
# message was lost (enqueue failed, Redis lost it) — see
# specs/upload-chunking.md §7.
STALE_PENDING_AFTER = timedelta(minutes=10)


class RequeueStaleUploads:
    """Recovers uploads whose job got lost or whose worker died. Postgres
    statuses are the source of truth, so this works whatever happened to
    the queue message. Re-enqueuing is safe: ProcessUpload's claim is
    atomic, so a job runs at most once at a time.

    A stale PROCESSING upload counts as a failed attempt — otherwise a file
    that crashes the worker every time (e.g. running out of memory) would be
    retried forever.
    """

    def __init__(
        self,
        uploaded_file_repo: UploadedFileRepository,
        unit_of_work: UnitOfWork,
        upload_queue: UploadQueue,
        clock: Clock,
    ) -> None:
        self._uploads = uploaded_file_repo
        self._uow = unit_of_work
        self._queue = upload_queue
        self._clock = clock

    def execute(self) -> list[int]:
        now = self._clock.now()
        stale = self._uploads.list_stale(
            pending_before=now - STALE_PENDING_AFTER,
            processing_before=now - STALE_PROCESSING_AFTER,
        )
        to_enqueue: list[int] = []
        for upload in stale:
            if upload.status == UploadStatus.PROCESSING:
                attempts = self._uploads.mark_retry(upload.id)
                if attempts >= MAX_ATTEMPTS:
                    self._uploads.mark_failed(upload.id, GAVE_UP_MESSAGE)
                    continue
            to_enqueue.append(upload.id)
        self._uow.commit()

        for upload_id in to_enqueue:
            try:
                self._queue.enqueue(upload_id)
            except Exception:  # noqa: BLE001 - next sweep tries again
                logger.exception("Could not re-enqueue upload %s", upload_id)
        return to_enqueue
