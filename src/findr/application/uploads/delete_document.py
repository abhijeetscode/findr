from findr.domain.exceptions import DocumentNotFound
from findr.ports.document_repository import DocumentRepository
from findr.ports.file_storage import FileStorage
from findr.ports.search_index import SearchIndex
from findr.ports.uploaded_file_repository import UploadedFileRepository


class DeleteDocument:
    """Removes one document from Postgres and the search index, plus its
    stored original if it was an upload. Generic over source type — see
    specs/file-upload.md §2.3."""

    def __init__(
        self,
        document_repo: DocumentRepository,
        uploaded_file_repo: UploadedFileRepository,
        search_index: SearchIndex,
        file_storage: FileStorage,
    ) -> None:
        self._document_repo = document_repo
        self._uploaded_file_repo = uploaded_file_repo
        self._search_index = search_index
        self._file_storage = file_storage

    def execute(self, document_id: int, user_id: int) -> None:
        document = self._document_repo.get(document_id, user_id)
        if document is None:
            raise DocumentNotFound(f"No document {document_id} for this user")

        uploaded_file = self._uploaded_file_repo.get_by_document_id(document_id)
        # uploaded_files.document_id references documents.id — child row first.
        if uploaded_file is not None:
            self._uploaded_file_repo.delete_by_document_id(document_id)
        self._document_repo.delete_by_id(document_id)
        self._search_index.delete_documents(document.connection_id, [document.external_id])

        # Last, so a failure in any step above leaves the original on disk
        # rather than a row pointing at a file that's already gone.
        if uploaded_file is not None:
            self._file_storage.delete(uploaded_file.storage_path)
