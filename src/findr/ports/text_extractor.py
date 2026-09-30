from typing import Protocol


class TextExtractor(Protocol):
    def extract(self, content: bytes, mime_type: str) -> str:
        """Raises UnsupportedFileType / ExtractionFailed rather than
        returning an empty string, so a bad upload is a clear 4xx, not a
        silently empty, unsearchable document."""
        ...
