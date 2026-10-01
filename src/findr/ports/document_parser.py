from typing import Protocol

from findr.domain.entities import ParsedDocument


class DocumentParser(Protocol):
    def parse(
        self,
        content: bytes,
        mime_type: str,
        filename: str,
        document_version: int,
        workspace_id: int,
    ) -> ParsedDocument:
        """Partitions and chunks one file. Raises UnsupportedFileType /
        ExtractionFailed rather than returning empty text. Slow (OCR, layout
        models) and memory-hungry: call from the background worker, never a
        request handler. See specs/upload-chunking.md §6."""
        ...
