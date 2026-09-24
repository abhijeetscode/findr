class DomainError(Exception):
    """Base class for domain-level errors."""


class InvalidCredentials(DomainError):
    """Raised when login credentials don't match a known user."""


class DuplicateUser(DomainError):
    """Raised when registering an email that's already taken."""


class ConnectionNotFound(DomainError):
    """Raised when a source connection doesn't exist for the given user."""


class SourceAuthError(DomainError):
    """Raised by a SourceConnector when stored credentials are invalid or expired."""


class InvalidOAuthState(DomainError):
    """Raised when an OAuth callback's state doesn't match a known, unexpired request."""


class SourceCursorExpired(DomainError):
    """Raised by a SourceConnector when its incremental sync cursor is too old
    to resume from (e.g. Gmail only retains ~7 days of history) — the caller
    should fall back to a full resync (cursor=None)."""
