from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from findr.adapters.outbound.postgres.models import UserModel
from findr.adapters.outbound.system_clock import SystemClock
from findr.domain.entities import User
from findr.ports.clock import Clock


class UserRepositoryPostgres:
    """Implements ports.user_repository.UserRepository."""

    def __init__(self, db: DbSession, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()

    def get_by_email(self, email: str) -> User | None:
        row = self._db.execute(select(UserModel).where(UserModel.email == email)).scalar_one_or_none()
        return _to_domain(row) if row is not None else None

    def get_by_id(self, user_id: int) -> User | None:
        row = self._db.get(UserModel, user_id)
        return _to_domain(row) if row is not None else None

    def create(self, email: str, password_hash: str) -> User:
        row = UserModel(email=email, password_hash=password_hash, created_at=self._clock.now())
        self._db.add(row)
        self._db.flush()
        return _to_domain(row)


def _to_domain(row: UserModel) -> User:
    return User(
        id=row.id,
        email=row.email,
        password_hash=row.password_hash,
        created_at=row.created_at,
    )
