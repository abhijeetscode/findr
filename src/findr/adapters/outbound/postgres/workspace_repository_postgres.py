from __future__ import annotations

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session as DbSession

from findr.adapters.outbound.postgres.models import WorkspaceModel
from findr.adapters.outbound.system_clock import SystemClock
from findr.domain.entities import Workspace
from findr.ports.clock import Clock


class WorkspaceRepositoryPostgres:
    """Implements ports.workspace_repository.WorkspaceRepository."""

    def __init__(self, db: DbSession, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()

    def create(self, user_id: int, name: str) -> Workspace:
        row = WorkspaceModel(user_id=user_id, name=name, created_at=self._clock.now())
        self._db.add(row)
        self._db.flush()
        return _to_domain(row)

    def get(self, workspace_id: int, user_id: int) -> Workspace | None:
        row = self._db.get(WorkspaceModel, workspace_id)
        if row is None or row.user_id != user_id:
            return None
        return _to_domain(row)

    def list_for_user(self, user_id: int) -> list[Workspace]:
        rows = self._db.execute(
            select(WorkspaceModel)
            .where(WorkspaceModel.user_id == user_id)
            .order_by(func.lower(WorkspaceModel.name), WorkspaceModel.id)
        ).scalars()
        return [_to_domain(r) for r in rows]

    def get_by_name(self, user_id: int, name: str) -> Workspace | None:
        row = self._db.execute(
            select(WorkspaceModel).where(
                WorkspaceModel.user_id == user_id,
                func.lower(WorkspaceModel.name) == name.lower(),
            )
        ).scalar_one_or_none()
        return _to_domain(row) if row is not None else None

    def rename(self, workspace_id: int, name: str) -> None:
        self._db.execute(
            update(WorkspaceModel).where(WorkspaceModel.id == workspace_id).values(name=name)
        )
        self._db.flush()

    def delete(self, workspace_id: int) -> None:
        self._db.execute(delete(WorkspaceModel).where(WorkspaceModel.id == workspace_id))
        self._db.flush()


def _to_domain(row: WorkspaceModel) -> Workspace:
    return Workspace(id=row.id, user_id=row.user_id, name=row.name, created_at=row.created_at)
