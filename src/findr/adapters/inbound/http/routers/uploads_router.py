from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from findr.adapters.inbound.http.deps import (
    get_current_user,
    get_db_session,
    get_file_storage,
    get_search_index,
)
from findr.adapters.outbound.elasticsearch.search_index_elasticsearch import ElasticsearchIndex
from findr.adapters.outbound.files.local_file_storage import LocalFileStorage
from findr.adapters.outbound.postgres.document_repository_postgres import (
    DocumentRepositoryPostgres,
)
from findr.adapters.outbound.postgres.uploaded_file_repository_postgres import (
    UploadedFileRepositoryPostgres,
)
from findr.application.uploads.delete_upload import DeleteUpload
from findr.application.uploads.list_uploads import GetUpload, ListUploads
from findr.domain.entities import UploadedFile, User
from findr.domain.exceptions import UploadNotFound

router = APIRouter(prefix="/uploads", tags=["uploads"])


class UploadStatusResponse(BaseModel):
    upload_id: int
    filename: str
    status: str
    error: str | None
    document_id: int | None
    created_at: datetime
    updated_at: datetime


def _to_response(upload: UploadedFile) -> UploadStatusResponse:
    return UploadStatusResponse(
        upload_id=upload.id,
        filename=upload.original_filename,
        status=upload.status.value,
        error=upload.error,
        document_id=upload.document_id,
        created_at=upload.created_at,
        updated_at=upload.updated_at,
    )


@router.get("", response_model=list[UploadStatusResponse])
def list_uploads(
    user: User = Depends(get_current_user), db: Session = Depends(get_db_session)
) -> list[UploadStatusResponse]:
    """The user's uploads, newest first, with their processing status —
    see specs/upload-chunking.md §8."""
    uploads = ListUploads(UploadedFileRepositoryPostgres(db)).execute(user.id)
    return [_to_response(u) for u in uploads]


@router.get("/{upload_id}", response_model=UploadStatusResponse)
def get_upload(
    upload_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
) -> UploadStatusResponse:
    try:
        upload = GetUpload(UploadedFileRepositoryPostgres(db)).execute(upload_id, user.id)
    except UploadNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found") from exc
    return _to_response(upload)


@router.delete("/{upload_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_upload(
    upload_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
    search_index: ElasticsearchIndex = Depends(get_search_index),
    file_storage: LocalFileStorage = Depends(get_file_storage),
) -> None:
    """Removes an upload in any status. 404s identically for "doesn't
    exist" and "belongs to someone else"."""
    use_case = DeleteUpload(
        UploadedFileRepositoryPostgres(db),
        DocumentRepositoryPostgres(db),
        search_index,
        file_storage,
    )
    try:
        use_case.execute(upload_id, user.id)
    except UploadNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found") from exc
    db.commit()
