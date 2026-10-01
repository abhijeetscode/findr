from findr.domain.exceptions import UploadNotFound
from findr.ports.document_repository import DocumentRepository
from findr.ports.file_storage import FileStorage
from findr.ports.search_index import SearchIndex
from findr.ports.uploaded_file_repository import UploadedFileRepository


class DeleteUpload:
    """Removes an upload in any status — pending, processing, ready or
    failed — with its document, chunks, index entry and stored file where
    they exist. See specs/upload-chunking.md §8.1."""

    def __init__(
        self,
        uploaded_file_repo: UploadedFileRepository,
        document_repo: DocumentRepository,
        search_index: SearchIndex,
        file_storage: FileStorage,
    ) -> None:
        self._uploads = uploaded_file_repo
        self._documents = document_repo
        self._search_index = search_index
        self._storage = file_storage

    def execute(self, upload_id: int, user_id: int) -> None:
        if self._uploads.get(upload_id, user_id) is None:
            raise UploadNotFound(f"No upload {upload_id} for this user")
        # Waits for a worker that's finishing this upload, so we see its
        # final document_id rather than racing it (ProcessUpload locks too).
        upload = self._uploads.lock(upload_id)
        if upload is None:
            return

        document = (
            self._documents.get(upload.document_id, user_id)
            if upload.document_id is not None
            else None
        )
        self._uploads.delete(upload_id)
        if document is not None:
            self._documents.delete_by_id(document.id)
            self._search_index.delete_documents(document.connection_id, [document.external_id])
        self._storage.delete(upload.storage_path)
