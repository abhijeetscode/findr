import uuid

from findr.domain.entities import Document, UploadedFile
from findr.domain.value_objects import SourceType
from findr.ports.document_repository import DocumentRepository
from findr.ports.file_storage import FileStorage
from findr.ports.search_index import SearchIndex
from findr.ports.source_connection_repo import SourceConnectionRepository
from findr.ports.text_extractor import TextExtractor
from findr.ports.uploaded_file_repository import UploadedFileRepository

UPLOADS_DISPLAY_NAME = "Uploaded files"


class UploadFile:
    """Push-based counterpart to SyncSource: an upload is a one-off user
    action with no OAuth and nothing to poll, so it's its own use case rather
    than a SourceConnector — see specs/file-upload.md §2/§4.

    Not transactional across storage/Postgres/Elasticsearch: if a step after
    FileStorage.save fails, the saved file is left orphaned on disk (accepted
    for the MVP, specs/file-upload.md §5). Extraction runs first, so a bad
    file never touches storage at all.
    """

    def __init__(
        self,
        text_extractor: TextExtractor,
        file_storage: FileStorage,
        connection_repo: SourceConnectionRepository,
        document_repo: DocumentRepository,
        uploaded_file_repo: UploadedFileRepository,
        search_index: SearchIndex,
    ) -> None:
        self._text_extractor = text_extractor
        self._file_storage = file_storage
        self._connection_repo = connection_repo
        self._document_repo = document_repo
        self._uploaded_file_repo = uploaded_file_repo
        self._search_index = search_index

    def execute(
        self, user_id: int, filename: str, mime_type: str, content: bytes
    ) -> tuple[Document, UploadedFile]:
        body_text = self._text_extractor.extract(content, mime_type)

        storage_path = self._file_storage.save(user_id, filename, content)

        connection = self._connection_repo.get_by_account(user_id, SourceType.FILE, None)
        if connection is None:
            connection = self._connection_repo.create(
                user_id, SourceType.FILE, None, display_name=UPLOADS_DISPLAY_NAME
            )

        document = Document(
            id=0,
            user_id=user_id,
            connection_id=connection.id,
            # Uploads have no natural external id; a fresh one per upload
            # means two files with the same name never collide.
            external_id=str(uuid.uuid4()),
            subject=filename,
            sender=None,
            recipients=None,
            body_text=body_text,
            sent_at=None,
            thread_id=None,
        )
        [document] = self._document_repo.upsert_many([document])

        uploaded_file = self._uploaded_file_repo.create(
            document_id=document.id,
            user_id=user_id,
            original_filename=filename,
            mime_type=mime_type,
            file_size_bytes=len(content),
            storage_path=storage_path,
        )

        self._search_index.index_documents([document], SourceType.FILE, None)
        return document, uploaded_file
