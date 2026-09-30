from enum import Enum


class SourceType(str, Enum):
    GMAIL = "gmail"
    SLACK = "slack"
    NOTION = "notion"
    # Uploaded files — push-based, no OAuth, never polled by the scheduler.
    # See specs/file-upload.md §2.
    FILE = "file"


class ConnectionStatus(str, Enum):
    ACTIVE = "active"
    NEEDS_REAUTH = "needs_reauth"
    ERROR = "error"
    DISCONNECTED = "disconnected"
