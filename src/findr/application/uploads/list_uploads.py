from findr.domain.entities import UploadedFile
from findr.domain.exceptions import UploadNotFound
from findr.ports.uploaded_file_repository import UploadedFileRepository


class ListUploads:
    def __init__(self, uploaded_file_repo: UploadedFileRepository) -> None:
        self._uploads = uploaded_file_repo

    def execute(self, user_id: int) -> list[UploadedFile]:
        return self._uploads.list_for_user(user_id)


class GetUpload:
    def __init__(self, uploaded_file_repo: UploadedFileRepository) -> None:
        self._uploads = uploaded_file_repo

    def execute(self, upload_id: int, user_id: int) -> UploadedFile:
        upload = self._uploads.get(upload_id, user_id)
        if upload is None:
            raise UploadNotFound(f"No upload {upload_id} for this user")
        return upload
