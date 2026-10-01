from datetime import datetime
from typing import Protocol

from findr.domain.entities import UploadedFile


class UploadedFileRepository(Protocol):
    def create(
        self,
        user_id: int,
        original_filename: str,
        mime_type: str,
        file_size_bytes: int,
        storage_path: str,
        content_sha256: str,
        document_version: int,
    ) -> UploadedFile:
        """Status PENDING, no document yet."""
        ...
    def get(self, upload_id: int, user_id: int) -> UploadedFile | None:
        """None both when missing and when it belongs to another user."""
        ...
    def get_by_document_id(self, document_id: int) -> UploadedFile | None: ...
    def list_for_user(self, user_id: int) -> list[UploadedFile]:
        """Newest first."""
        ...
    def claim(self, upload_id: int, stale_before: datetime) -> UploadedFile | None:
        """Atomically moves PENDING → PROCESSING, or re-claims a PROCESSING
        row not updated since `stale_before` (a crashed worker's). Returns
        None if it isn't claimable: already READY/FAILED, being processed,
        or deleted."""
        ...
    def lock(self, upload_id: int) -> UploadedFile | None:
        """Row-locks the upload for the rest of the transaction; None if it
        was deleted."""
        ...
    def mark_ready(self, upload_id: int, document_id: int) -> None: ...
    def mark_failed(self, upload_id: int, error: str) -> None: ...
    def mark_retry(self, upload_id: int) -> int:
        """Back to PENDING with attempts + 1; returns the new attempt count."""
        ...
    def list_stale(
        self, pending_before: datetime, processing_before: datetime
    ) -> list[UploadedFile]: ...
    def delete(self, upload_id: int) -> None: ...
