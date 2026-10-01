import hashlib
import logging

from findr.domain.entities import UploadedFile
from findr.domain.exceptions import ExtractionFailed, UnsupportedFileType
from findr.ports.file_storage import FileStorage
from findr.ports.unit_of_work import UnitOfWork
from findr.ports.upload_queue import UploadQueue
from findr.ports.uploaded_file_repository import UploadedFileRepository

logger = logging.getLogger(__name__)

UPLOADS_DISPLAY_NAME = "Uploaded files"

# Until duplicate uploads are designed, every upload is version 1
# (specs/upload-chunking.md §6.3).
INITIAL_DOCUMENT_VERSION = 1


class UploadFile:
    """The request half of an upload: validate, store, record as PENDING,
    enqueue. Parsing/OCR/chunking/indexing happen later in ProcessUpload, on
    the background worker — see specs/upload-chunking.md §5.4.

    Only cheap, certain checks happen here (unsupported type, empty file);
    anything that needs parsing becomes a FAILED status instead.
    """

    def __init__(
        self,
        supported_mime_types: frozenset[str],
        file_storage: FileStorage,
        uploaded_file_repo: UploadedFileRepository,
        unit_of_work: UnitOfWork,
        upload_queue: UploadQueue,
    ) -> None:
        self._supported_mime_types = supported_mime_types
        self._file_storage = file_storage
        self._uploaded_file_repo = uploaded_file_repo
        self._unit_of_work = unit_of_work
        self._upload_queue = upload_queue

    def execute(self, user_id: int, filename: str, mime_type: str, content: bytes) -> UploadedFile:
        if mime_type not in self._supported_mime_types:
            raise UnsupportedFileType(
                f"Unsupported file type {mime_type!r}; upload a PDF, DOCX, TXT or Markdown file"
            )
        if not content:
            raise ExtractionFailed("The file is empty")

        storage_path = self._file_storage.save(user_id, filename, content)
        upload = self._uploaded_file_repo.create(
            user_id=user_id,
            original_filename=filename,
            mime_type=mime_type,
            file_size_bytes=len(content),
            storage_path=storage_path,
            content_sha256=hashlib.sha256(content).hexdigest(),
            document_version=INITIAL_DOCUMENT_VERSION,
        )
        # Commit before enqueuing, so a worker can never receive an id whose
        # row isn't visible yet.
        self._unit_of_work.commit()
        try:
            self._upload_queue.enqueue(upload.id)
        except Exception:  # noqa: BLE001 - the stale-upload sweep re-enqueues it
            logger.exception("Could not enqueue upload %s; the sweep will retry", upload.id)
        return upload
