from typing import Protocol

from findr.domain.entities import UploadedFile


class UploadedFileRepository(Protocol):
    def create(
        self,
        document_id: int,
        user_id: int,
        original_filename: str,
        mime_type: str,
        file_size_bytes: int,
        storage_path: str,
    ) -> UploadedFile: ...
    def get_by_document_id(self, document_id: int) -> UploadedFile | None: ...
    def delete_by_document_id(self, document_id: int) -> None: ...
