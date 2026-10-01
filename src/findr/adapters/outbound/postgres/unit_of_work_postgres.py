from __future__ import annotations

from sqlalchemy.orm import Session as DbSession


class UnitOfWorkPostgres:
    """Implements ports.unit_of_work.UnitOfWork over one SQLAlchemy session —
    the same session the repositories in that use case share."""

    def __init__(self, db: DbSession) -> None:
        self._db = db

    def commit(self) -> None:
        self._db.commit()

    def rollback(self) -> None:
        self._db.rollback()
