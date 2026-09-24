from enum import Enum


class SourceType(str, Enum):
    GMAIL = "gmail"


class ConnectionStatus(str, Enum):
    ACTIVE = "active"
    NEEDS_REAUTH = "needs_reauth"
    ERROR = "error"
    DISCONNECTED = "disconnected"
