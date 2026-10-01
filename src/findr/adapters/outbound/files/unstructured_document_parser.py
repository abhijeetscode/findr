from __future__ import annotations

import io
from importlib.metadata import version

from findr.adapters.outbound.files.mime_types import DOCX, MARKDOWN, PDF, TEXT
from findr.domain.entities import ChunkMetadata, DocumentChunk, ParsedDocument
from findr.domain.exceptions import ExtractionFailed, UnsupportedFileType
from findr.domain.value_objects import ChunkKind

# chunk_by_title parameters — see specs/upload-chunking.md §6.2. Bump
# CHUNKING_VERSION whenever these or the partition settings change, so
# chunks made with older settings can be found (ChunkMetadata.parser_version).
CHUNKING_VERSION = "chunking-v1"
MAX_CHARACTERS = 1500
NEW_AFTER_N_CHARS = 1200
OVERLAP = 150
COMBINE_TEXT_UNDER_N_CHARS = 200

# OCR language for PDFs (tesseract). English only for now (spec §1).
OCR_LANGUAGES = ["eng"]

_TABLE_CATEGORIES = frozenset({"Table", "TableChunk"})

NO_TEXT_MESSAGE = "No text could be extracted from this file, even with OCR"


class UnstructuredDocumentParser:
    """Implements ports.document_parser.DocumentParser with the
    `unstructured` library: one explicit partition function per type (the
    auto-detecting partition() needs libmagic), then chunk_by_title.

    PDFs always use the hi_res strategy with table-structure inference:
    that's what gives OCR for scanned pages and table detection (the fast
    strategy finds no tables). It needs tesseract and poppler installed and
    downloads layout/table models on first use. See
    specs/upload-chunking.md §6.
    """

    def __init__(self) -> None:
        self._parser_version = f"unstructured-{version('unstructured')}/{CHUNKING_VERSION}"

    def warm_up(self) -> None:
        """Loads the PDF layout and table-structure models (downloading them
        on first run) and the spaCy model, so a problem shows up at worker
        startup rather than mid-upload. This matters beyond speed: if the
        table model can't load during a real parse, unstructured doesn't
        raise — it silently drops the table's content from the document."""
        import spacy
        from unstructured_inference.models import tables
        from unstructured_inference.models.base import get_model

        get_model()
        tables.load_agent()
        spacy.load("en_core_web_sm")

    def parse(
        self,
        content: bytes,
        mime_type: str,
        filename: str,
        document_version: int,
        workspace_id: int,
    ) -> ParsedDocument:
        elements = self._partition(content, mime_type)

        text = "\n\n".join(el.text.strip() for el in elements if el.text and el.text.strip())
        if not text:
            raise ExtractionFailed(NO_TEXT_MESSAGE)

        from unstructured.chunking.title import chunk_by_title

        raw_chunks = chunk_by_title(
            elements,
            max_characters=MAX_CHARACTERS,
            new_after_n_chars=NEW_AFTER_N_CHARS,
            overlap=OVERLAP,
            combine_text_under_n_chars=COMBINE_TEXT_UNDER_N_CHARS,
        )
        sections = _section_titles_by_element_id(elements)
        content_sha256 = _sha256(content)

        chunks: list[DocumentChunk] = []
        for raw in raw_chunks:
            chunk_text = (raw.text or "").strip()
            if not chunk_text:
                continue
            chunks.append(
                self._to_chunk(
                    raw,
                    chunk_text,
                    chunk_index=len(chunks),
                    sections=sections,
                    document_version=document_version,
                    content_sha256=content_sha256,
                    workspace_id=workspace_id,
                    filename=filename,
                    mime_type=mime_type,
                )
            )
        if not chunks:
            raise ExtractionFailed(NO_TEXT_MESSAGE)
        return ParsedDocument(text=text, chunks=chunks)

    def _partition(self, content: bytes, mime_type: str) -> list:
        if mime_type not in (PDF, DOCX, MARKDOWN, TEXT):
            raise UnsupportedFileType(
                f"Unsupported file type {mime_type!r}; upload a PDF, DOCX, TXT or Markdown file"
            )
        try:
            if mime_type == PDF:
                from unstructured.partition.pdf import partition_pdf

                return partition_pdf(
                    file=io.BytesIO(content),
                    strategy="hi_res",
                    infer_table_structure=True,
                    languages=OCR_LANGUAGES,
                )
            if mime_type == DOCX:
                from unstructured.partition.docx import partition_docx

                return partition_docx(file=io.BytesIO(content))
            if mime_type == MARKDOWN:
                from unstructured.partition.md import partition_md

                return partition_md(text=_decode(content))
            from unstructured.partition.text import partition_text

            return partition_text(text=_decode(content))
        except (ExtractionFailed, UnsupportedFileType):
            raise
        except OSError:
            # Environment problems — tesseract/poppler missing, a model
            # download failing, disk errors — aren't the file's fault.
            # Re-raised so the worker retries rather than failing the upload.
            raise
        except Exception as exc:  # noqa: BLE001 - corrupt/unreadable file
            raise ExtractionFailed(f"Could not read this file: {exc}") from exc

    def _to_chunk(
        self,
        raw,
        chunk_text: str,
        *,
        chunk_index: int,
        sections: dict[str, str | None],
        document_version: int,
        content_sha256: str,
        workspace_id: int,
        filename: str,
        mime_type: str,
    ) -> DocumentChunk:
        orig = list(raw.metadata.orig_elements or [])
        pages = sorted(
            {el.metadata.page_number for el in orig if el.metadata.page_number is not None}
        )
        element_types = list(dict.fromkeys(el.category for el in orig))
        section_title = sections.get(orig[0].id) if orig else None
        is_table = raw.category in _TABLE_CATEGORIES
        return DocumentChunk(
            text=chunk_text,
            kind=ChunkKind.TABLE if is_table else ChunkKind.TEXT,
            table_html=raw.metadata.text_as_html if is_table else None,
            metadata=ChunkMetadata(
                chunk_index=chunk_index,
                document_version=document_version,
                content_sha256=content_sha256,
                workspace_id=workspace_id,
                filename=filename,
                mime_type=mime_type,
                page_start=pages[0] if pages else None,
                page_end=pages[-1] if pages else None,
                section_title=section_title,
                element_types=element_types,
                languages=list(raw.metadata.languages or []),
                # Only present (True) on the 2nd+ piece of a split element.
                is_continuation=bool(raw.metadata.is_continuation),
                parser_version=self._parser_version,
            ),
        )


def _section_titles_by_element_id(elements: list) -> dict[str, str | None]:
    """unstructured has no "section" field: walk the elements in order and
    remember the most recent Title each element falls under."""
    current: str | None = None
    sections: dict[str, str | None] = {}
    for el in elements:
        if el.category == "Title" and el.text and el.text.strip():
            current = el.text.strip()
        sections[el.id] = current
    return sections


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExtractionFailed("Text file is not valid UTF-8") from exc


def _sha256(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()
