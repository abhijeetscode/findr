from __future__ import annotations

from pathlib import PurePath

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT = "text/plain"
MARKDOWN = "text/markdown"

SUPPORTED_MIME_TYPES = frozenset({PDF, DOCX, TEXT, MARKDOWN})

_MIME_BY_EXTENSION = {
    ".pdf": PDF,
    ".docx": DOCX,
    ".txt": TEXT,
    ".md": MARKDOWN,
    ".markdown": MARKDOWN,
}


def resolve_mime_type(filename: str, declared: str | None) -> str:
    """Browsers are inconsistent about Content-Type for uploads — .md in
    particular often arrives as application/octet-stream or empty — so a
    declared type we support wins, and otherwise the extension decides.
    Returns the declared (or octet-stream) type unchanged when neither is
    recognised, so the caller can reject it as unsupported."""
    declared = (declared or "").split(";", 1)[0].strip().lower()
    if declared in SUPPORTED_MIME_TYPES:
        return declared
    by_extension = _MIME_BY_EXTENSION.get(PurePath(filename).suffix.lower())
    return by_extension or declared or "application/octet-stream"
