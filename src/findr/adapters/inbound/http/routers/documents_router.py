from fastapi import APIRouter, Depends, HTTPException, status
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
from findr.application.uploads.delete_document import DeleteDocument
from findr.domain.entities import User
from findr.domain.exceptions import DocumentNotFound

router = APIRouter(prefix="/documents", tags=["documents"])


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
    search_index: ElasticsearchIndex = Depends(get_search_index),
    file_storage: LocalFileStorage = Depends(get_file_storage),
) -> None:
    """Removes one document — generic over source type, though only
    uploaded files expose it in the UI for now (specs/file-upload.md §2.3).
    404s identically for "doesn't exist" and "belongs to someone else"."""
    use_case = DeleteDocument(
        DocumentRepositoryPostgres(db),
        UploadedFileRepositoryPostgres(db),
        search_index,
        file_storage,
    )
    try:
        use_case.execute(document_id, user.id)
    except DocumentNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found") from exc
    db.commit()
