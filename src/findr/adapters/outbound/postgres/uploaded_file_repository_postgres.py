from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from findr.adapters.outbound.postgres.models import UploadedFileModel
from findr.adapters.outbound.system_clock import SystemClock
from findr.domain.entities import UploadedFile
from findr.ports.clock import Clock


class UploadedFileRepositoryPostgres:
    """Implements ports.uploaded_file_repository.UploadedFileRepository."""

    def __init__(self, db: DbSession, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()

    def create(
        self,
        document_id: int,
        user_id: int,
        original_filename: str,
        mime_type: str,
        file_size_bytes: int,
        storage_path: str,
    ) -> UploadedFile:
        row = UploadedFileModel(
            document_id=document_id,
            user_id=user_id,
            original_filename=original_filename,
            mime_type=mime_type,
            file_size_bytes=file_size_bytes,
            storage_path=storage_path,
            created_at=self._clock.now(),
        )
        self._db.add(row)
        self._db.flush()
        return _to_domain(row)

    def get_by_document_id(self, document_id: int) -> UploadedFile | None:
        row = self._db.execute(
            select(UploadedFileModel).where(UploadedFileModel.document_id == document_id)
        ).scalar_one_or_none()
        return _to_domain(row) if row is not None else None

    def delete_by_document_id(self, document_id: int) -> None:
        self._db.execute(
            delete(UploadedFileModel).where(UploadedFileModel.document_id == document_id)
        )
        self._db.flush()


def _to_domain(row: UploadedFileModel) -> UploadedFile:
    return UploadedFile(
        id=row.id,
        document_id=row.document_id,
        user_id=row.user_id,
        original_filename=row.original_filename,
        mime_type=row.mime_type,
        file_size_bytes=row.file_size_bytes,
        storage_path=row.storage_path,
        created_at=row.created_at,
    )
