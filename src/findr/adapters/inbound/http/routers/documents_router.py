from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from findr.adapters.inbound.http.deps import (
    get_current_user,
    get_db_session,
    get_file_storage,
    get_search_index,
)
from findr.adapters.outbound.elasticsearch.search_index_elasticsearch import ElasticsearchIndex
from findr.adapters.outbound.files.local_file_storage import LocalFileStorage
from findr.adapters.outbound.files.mime_types import MARKDOWN, TEXT
from findr.adapters.outbound.postgres.document_repository_postgres import (
    DocumentRepositoryPostgres,
)
from findr.adapters.outbound.postgres.uploaded_file_repository_postgres import (
    UploadedFileRepositoryPostgres,
)
from findr.application.uploads.delete_document import DeleteDocument
from findr.application.uploads.get_document_file import GetDocumentFile
from findr.domain.entities import User
from findr.domain.exceptions import DocumentNotFound

router = APIRouter(prefix="/documents", tags=["documents"])

# Text uploads are always served as plain text, never as their own type: a
# browser could otherwise render HTML inside a Markdown file in our origin.
_PLAIN_TEXT_TYPES = frozenset({TEXT, MARKDOWN})


@router.get("/{document_id}/file")
def get_document_file(
    document_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
    file_storage: LocalFileStorage = Depends(get_file_storage),
) -> Response:
    """The original uploaded file, opened in a new tab from a search result
    (specs/open-files-and-pdf-pages.md §3.1). 404s identically for "doesn't
    exist", "belongs to someone else" and "isn't an uploaded file"."""
    use_case = GetDocumentFile(UploadedFileRepositoryPostgres(db), file_storage)
    try:
        file = use_case.execute(document_id, user.id)
    except DocumentNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found") from exc
    media_type = (
        "text/plain; charset=utf-8" if file.mime_type in _PLAIN_TEXT_TYPES else file.mime_type
    )
    return Response(
        content=file.content,
        media_type=media_type,
        headers={
            "Content-Disposition": _inline_disposition(file.filename),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


def _inline_disposition(filename: str) -> str:
    """RFC 6266: a plain-ASCII `filename` for old clients plus a UTF-8
    `filename*` that carries the real name."""
    ascii_fallback = "".join(
        ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in filename
    )
    return f"inline; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


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
