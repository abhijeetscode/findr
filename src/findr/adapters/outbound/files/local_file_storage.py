from __future__ import annotations

import re
import uuid
from pathlib import Path

# Only a short, plain extension survives from the user-supplied filename —
# the on-disk name is always a fresh UUID, so a hostile filename can't
# traverse out of the storage root or collide with another upload.
_SAFE_EXTENSION = re.compile(r"^\.[a-z0-9]{1,10}$")


class LocalFileStorage:
    """Implements ports.file_storage.FileStorage on local disk (a Docker
    volume in deployment). Layout: {root}/{user_id}/{uuid4}{ext}; the
    returned storage_path is that path relative to root. See
    specs/file-upload.md §3."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()

    def save(self, user_id: int, filename: str, content: bytes) -> str:
        extension = Path(filename).suffix.lower()
        if not _SAFE_EXTENSION.match(extension):
            extension = ""
        storage_path = f"{user_id}/{uuid.uuid4()}{extension}"
        target = self._resolve(storage_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return storage_path

    def read(self, storage_path: str) -> bytes:
        return self._resolve(storage_path).read_bytes()

    def delete(self, storage_path: str) -> None:
        self._resolve(storage_path).unlink(missing_ok=True)

    def _resolve(self, storage_path: str) -> Path:
        path = (self._root / storage_path).resolve()
        if not path.is_relative_to(self._root):
            raise ValueError(f"storage_path escapes the storage root: {storage_path!r}")
        return path
