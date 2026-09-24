from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy.orm import Session as DbSession

from findr.adapters.outbound.sqlite.models import OAuthStateModel
from findr.adapters.outbound.system_clock import SystemClock
from findr.ports.clock import Clock
from findr.ports.oauth_state_repository import OAuthState


class OAuthStateRepositorySqlite:
    """Implements ports.oauth_state_repository.OAuthStateRepository."""

    def __init__(self, db: DbSession, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()

    def create(self, user_id: int, code_verifier: str, ttl_seconds: int) -> str:
        state = secrets.token_urlsafe(32)
        now = self._clock.now()
        row = OAuthStateModel(
            state=state,
            user_id=user_id,
            code_verifier=code_verifier,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        self._db.add(row)
        self._db.flush()
        return state

    def consume(self, state: str) -> OAuthState | None:
        row = self._db.get(OAuthStateModel, state)
        if row is None:
            return None
        # One-time use: delete immediately regardless of expiry outcome, so
        # a state token can never be replayed either way.
        result = OAuthState(
            state=row.state,
            user_id=row.user_id,
            code_verifier=row.code_verifier,
            expires_at=row.expires_at,
        )
        self._db.delete(row)
        self._db.flush()
        if result.expires_at < self._clock.now():
            return None
        return result
