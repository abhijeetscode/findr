from dataclasses import dataclass

from findr.domain.exceptions import DocumentNotFound
from findr.ports.file_storage import FileStorage
from findr.ports.uploaded_file_repository import UploadedFileRepository


@dataclass(frozen=True)
class DocumentFile:
    content: bytes
    mime_type: str
    filename: str


class GetDocumentFile:
    """The original uploaded file behind a document, for opening it from a
    search result — see specs/open-files-and-pdf-pages.md §3.1."""

    def __init__(self, uploaded_file_repo: UploadedFileRepository, file_storage: FileStorage) -> None:
        self._uploaded_file_repo = uploaded_file_repo
        self._file_storage = file_storage

    def execute(self, document_id: int, user_id: int) -> DocumentFile:
        uploaded_file = self._uploaded_file_repo.get_by_document_id(document_id)
        # Missing, someone else's, and not an upload (e.g. a Gmail message)
        # are indistinguishable to the caller.
        if uploaded_file is None or uploaded_file.user_id != user_id:
            raise DocumentNotFound(f"No uploaded file for document {document_id} for this user")
        return DocumentFile(
            content=self._file_storage.read(uploaded_file.storage_path),
            mime_type=uploaded_file.mime_type,
            filename=uploaded_file.original_filename,
        )
