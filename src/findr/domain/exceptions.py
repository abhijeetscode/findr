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
