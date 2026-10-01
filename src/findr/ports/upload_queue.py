from typing import Protocol


class UploadQueue(Protocol):
    def enqueue(self, upload_id: int) -> None:
        """Asks a worker to process this upload. Only the id travels — the
        worker reads everything else from Postgres and FileStorage — so a
        duplicate message is harmless (the worker's claim is atomic). See
        specs/upload-chunking.md §4.4."""
        ...
