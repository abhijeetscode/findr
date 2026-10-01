from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy.orm import Session

from findr.adapters.inbound.http.deps import (
    get_current_user,
    get_db_session,
    get_file_storage,
    get_search_index,
    get_upload_queue,
    get_workspace,
)
from findr.adapters.outbound.elasticsearch.search_index_elasticsearch import ElasticsearchIndex
from findr.adapters.outbound.files.local_file_storage import LocalFileStorage
from findr.adapters.outbound.files.mime_types import SUPPORTED_MIME_TYPES, resolve_mime_type
from findr.adapters.outbound.postgres.document_repository_postgres import (
    DocumentRepositoryPostgres,
)
from findr.adapters.outbound.postgres.unit_of_work_postgres import UnitOfWorkPostgres
from findr.adapters.outbound.postgres.uploaded_file_repository_postgres import (
    UploadedFileRepositoryPostgres,
)
from findr.application.uploads.delete_upload import DeleteUpload
from findr.application.uploads.list_uploads import GetUpload, ListUploads
from findr.application.uploads.upload_file import UploadFile as UploadFileUseCase
from findr.domain.entities import UploadedFile, User, Workspace
from findr.domain.exceptions import ExtractionFailed, UnsupportedFileType, UploadNotFound
from findr.ports.upload_queue import UploadQueue

# Item routes: an upload's id already pins down its workspace.
router = APIRouter(prefix="/uploads", tags=["uploads"])
# Workspace-scoped routes (specs/workspaces.md §6).
workspace_router = APIRouter(prefix="/workspaces/{workspace_id}/uploads", tags=["uploads"])


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


class UploadResponse(BaseModel):
    upload_id: int
    filename: str
    mime_type: str
    file_size_bytes: int
    status: str


@workspace_router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=UploadResponse)
async def upload_file(
    file: UploadFile,
    workspace: Workspace = Depends(get_workspace),
    db: Session = Depends(get_db_session),
    file_storage: LocalFileStorage = Depends(get_file_storage),
    upload_queue: UploadQueue = Depends(get_upload_queue),
) -> UploadResponse:
    """Stores one file in this workspace and queues it for background
    processing (OCR, tables, chunking, indexing) — see
    specs/upload-chunking.md. Track it with GET /uploads/{upload_id}."""
    filename = file.filename or "untitled"
    content = await file.read()
    use_case = UploadFileUseCase(
        SUPPORTED_MIME_TYPES,
        file_storage,
        UploadedFileRepositoryPostgres(db),
        UnitOfWorkPostgres(db),
        upload_queue,
    )
    try:
        # In a thread: the queue adapter hands its coroutine to this event
        # loop and waits for it, which would deadlock on the loop itself.
        upload = await run_in_threadpool(
            use_case.execute,
            workspace.user_id,
            workspace.id,
            filename,
            resolve_mime_type(filename, file.content_type),
            content,
        )
    except (UnsupportedFileType, ExtractionFailed) as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return UploadResponse(
        upload_id=upload.id,
        filename=upload.original_filename,
        mime_type=upload.mime_type,
        file_size_bytes=upload.file_size_bytes,
        status=upload.status.value,
    )


@workspace_router.get("", response_model=list[UploadStatusResponse])
def list_uploads(
    workspace: Workspace = Depends(get_workspace), db: Session = Depends(get_db_session)
) -> list[UploadStatusResponse]:
    """The workspace's uploads, newest first, with their processing status —
    see specs/upload-chunking.md §8."""
    uploads = ListUploads(UploadedFileRepositoryPostgres(db)).execute(workspace.id)
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
