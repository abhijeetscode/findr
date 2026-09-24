from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class OAuthState:
    state: str
    user_id: int
    code_verifier: str
    expires_at: datetime


class OAuthStateRepository(Protocol):
    def create(self, user_id: int, code_verifier: str, ttl_seconds: int) -> str:
        """Persists a pending OAuth request and returns its state token."""
        ...

    def consume(self, state: str) -> OAuthState | None:
        """One-time read: the record is deleted whether found or not, so a
        state token can never be replayed. Returns None if unknown or
        expired."""
        ...
