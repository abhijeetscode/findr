from typing import Protocol


class FileStorage(Protocol):
    """Raw bytes of uploaded files. A port so LocalFileStorage can be swapped
    for an S3-compatible adapter later — see specs/file-upload.md §3/§9."""

    def save(self, user_id: int, filename: str, content: bytes) -> str:
        """Persists the file, returns a storage_path/key to read or delete it
        by later. `filename` is only used for its extension — never as the
        on-disk name."""
        ...

    def read(self, storage_path: str) -> bytes: ...

    def delete(self, storage_path: str) -> None:
        """Idempotent: deleting an already-missing file is not an error."""
        ...
