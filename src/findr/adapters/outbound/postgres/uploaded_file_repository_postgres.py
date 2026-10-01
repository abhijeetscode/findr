from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.orm import Session as DbSession

from findr.adapters.outbound.postgres.models import UploadedFileModel
from findr.adapters.outbound.system_clock import SystemClock
from findr.domain.entities import UploadedFile
from findr.domain.value_objects import UploadStatus
from findr.ports.clock import Clock


class UploadedFileRepositoryPostgres:
    """Implements ports.uploaded_file_repository.UploadedFileRepository."""

    def __init__(self, db: DbSession, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()

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
        now = self._clock.now()
        row = UploadedFileModel(
            document_id=None,
            user_id=user_id,
            original_filename=original_filename,
            mime_type=mime_type,
            file_size_bytes=file_size_bytes,
            storage_path=storage_path,
            content_sha256=content_sha256,
            document_version=document_version,
            status=UploadStatus.PENDING.value,
            error=None,
            attempts=0,
            created_at=now,
            updated_at=now,
        )
        self._db.add(row)
        self._db.flush()
        return _to_domain(row)

    def get(self, upload_id: int, user_id: int) -> UploadedFile | None:
        row = self._db.get(UploadedFileModel, upload_id)
        if row is None or row.user_id != user_id:
            return None
        return _to_domain(row)

    def get_by_document_id(self, document_id: int) -> UploadedFile | None:
        row = self._db.execute(
            select(UploadedFileModel).where(UploadedFileModel.document_id == document_id)
        ).scalar_one_or_none()
        return _to_domain(row) if row is not None else None

    def list_for_user(self, user_id: int) -> list[UploadedFile]:
        rows = self._db.execute(
            select(UploadedFileModel)
            .where(UploadedFileModel.user_id == user_id)
            .order_by(UploadedFileModel.created_at.desc(), UploadedFileModel.id.desc())
        ).scalars()
        return [_to_domain(r) for r in rows]

    def claim(self, upload_id: int, stale_before: datetime) -> UploadedFile | None:
        # One UPDATE ... RETURNING: Postgres serialises concurrent claims on
        # the row, so exactly one worker wins even if the same message is
        # delivered twice.
        row = self._db.execute(
            update(UploadedFileModel)
            .where(
                UploadedFileModel.id == upload_id,
                or_(
                    UploadedFileModel.status == UploadStatus.PENDING.value,
                    and_(
                        UploadedFileModel.status == UploadStatus.PROCESSING.value,
                        UploadedFileModel.updated_at < stale_before,
                    ),
                ),
            )
            .values(status=UploadStatus.PROCESSING.value, updated_at=self._clock.now())
            .returning(UploadedFileModel)
        ).scalar_one_or_none()
        self._db.flush()
        return _to_domain(row) if row is not None else None

    def lock(self, upload_id: int) -> UploadedFile | None:
        row = self._db.execute(
            select(UploadedFileModel)
            .where(UploadedFileModel.id == upload_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        return _to_domain(row) if row is not None else None

    def mark_ready(self, upload_id: int, document_id: int) -> None:
        self._set(upload_id, status=UploadStatus.READY.value, document_id=document_id, error=None)

    def mark_failed(self, upload_id: int, error: str) -> None:
        self._set(upload_id, status=UploadStatus.FAILED.value, error=error)

    def mark_retry(self, upload_id: int) -> int:
        attempts = self._db.execute(
            update(UploadedFileModel)
            .where(UploadedFileModel.id == upload_id)
            .values(
                status=UploadStatus.PENDING.value,
                attempts=UploadedFileModel.attempts + 1,
                updated_at=self._clock.now(),
            )
            .returning(UploadedFileModel.attempts)
        ).scalar_one_or_none()
        self._db.flush()
        return attempts or 0

    def list_stale(
        self, pending_before: datetime, processing_before: datetime
    ) -> list[UploadedFile]:
        rows = self._db.execute(
            select(UploadedFileModel).where(
                or_(
                    and_(
                        UploadedFileModel.status == UploadStatus.PENDING.value,
                        UploadedFileModel.updated_at < pending_before,
                    ),
                    and_(
                        UploadedFileModel.status == UploadStatus.PROCESSING.value,
                        UploadedFileModel.updated_at < processing_before,
                    ),
                )
            )
        ).scalars()
        return [_to_domain(r) for r in rows]

    def delete(self, upload_id: int) -> None:
        self._db.execute(delete(UploadedFileModel).where(UploadedFileModel.id == upload_id))
        self._db.flush()

    def _set(self, upload_id: int, **values) -> None:
        self._db.execute(
            update(UploadedFileModel)
            .where(UploadedFileModel.id == upload_id)
            .values(updated_at=self._clock.now(), **values)
        )
        self._db.flush()


def _to_domain(row: UploadedFileModel) -> UploadedFile:
    return UploadedFile(
        id=row.id,
        user_id=row.user_id,
        original_filename=row.original_filename,
        mime_type=row.mime_type,
        file_size_bytes=row.file_size_bytes,
        storage_path=row.storage_path,
        content_sha256=row.content_sha256,
        document_version=row.document_version,
        status=UploadStatus(row.status),
        document_id=row.document_id,
        error=row.error,
        attempts=row.attempts,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
