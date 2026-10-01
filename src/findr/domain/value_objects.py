from enum import Enum


class SourceType(str, Enum):
    GMAIL = "gmail"
    # Uploaded files — push-based, no OAuth, never polled by the scheduler.
    # See specs/file-upload.md §2.
    FILE = "file"


class ChunkKind(str, Enum):
    TEXT = "text"
    TABLE = "table"


class UploadStatus(str, Enum):
    """Lifecycle of an upload through the background worker — see
    specs/upload-chunking.md §3."""

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class ConnectionStatus(str, Enum):
    ACTIVE = "active"
    NEEDS_REAUTH = "needs_reauth"
    ERROR = "error"
    DISCONNECTED = "disconnected"
