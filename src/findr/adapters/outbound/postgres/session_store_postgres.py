from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session as DbSession

from findr.adapters.outbound.postgres.models import SessionModel
from findr.adapters.outbound.system_clock import SystemClock
from findr.ports.clock import Clock

DEFAULT_SESSION_TTL_DAYS = 14


class SessionStorePostgres:
    """Implements ports.session_store.SessionStore."""

    def __init__(
        self,
        db: DbSession,
        clock: Clock | None = None,
        ttl_days: int = DEFAULT_SESSION_TTL_DAYS,
    ) -> None:
        self._db = db
        self._clock = clock or SystemClock()
        self._ttl = timedelta(days=ttl_days)

    def create(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock.now()
        row = SessionModel(
            token=token, user_id=user_id, created_at=now, expires_at=now + self._ttl
        )
        self._db.add(row)
        self._db.flush()
        return token

    def get_user_id(self, token: str) -> int | None:
        row = self._db.get(SessionModel, token)
        if row is None:
            return None
        if row.expires_at < self._clock.now():
            self._db.delete(row)
            self._db.flush()
            return None
        return row.user_id

    def delete(self, token: str) -> None:
        self._db.execute(delete(SessionModel).where(SessionModel.token == token))
        self._db.flush()
