from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from findr.adapters.outbound.postgres.models import SourceConnectionModel
from findr.adapters.outbound.system_clock import SystemClock
from findr.domain.entities import SourceConnection
from findr.domain.value_objects import ConnectionStatus, SourceType
from findr.ports.clock import Clock


class SourceConnectionRepositoryPostgres:
    """Implements ports.source_connection_repo.SourceConnectionRepository."""

    def __init__(self, db: DbSession, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()

    def create(
        self,
        user_id: int,
        source_type: SourceType,
        external_account: str | None,
        display_name: str | None = None,
    ) -> SourceConnection:
        row = SourceConnectionModel(
            user_id=user_id,
            source_type=source_type.value,
            external_account=external_account,
            display_name=display_name,
            status=ConnectionStatus.ACTIVE.value,
            access_token_enc=None,
            refresh_token_enc=None,
            token_expires_at=None,
            sync_cursor=None,
            last_synced_at=None,
            last_error=None,
            created_at=self._clock.now(),
        )
        self._db.add(row)
        self._db.flush()
        return _to_domain(row)

    def get(self, connection_id: int, user_id: int) -> SourceConnection | None:
        row = self._db.get(SourceConnectionModel, connection_id)
        if row is None or row.user_id != user_id:
            return None
        return _to_domain(row)

    def get_by_account(
        self, user_id: int, source_type: SourceType, external_account: str | None
    ) -> SourceConnection | None:
        # IS NULL, not "= NULL", for the FILE connection's absent account.
        # Postgres treats NULLs as distinct in the (user_id, source_type,
        # external_account) unique constraint, so two concurrent first
        # uploads could each create a FILE connection — .first() keeps that
        # from turning into a MultipleResultsFound on every later upload.
        account_clause = (
            SourceConnectionModel.external_account.is_(None)
            if external_account is None
            else SourceConnectionModel.external_account == external_account
        )
        row = (
            self._db.execute(
                select(SourceConnectionModel)
                .where(
                    SourceConnectionModel.user_id == user_id,
                    SourceConnectionModel.source_type == source_type.value,
                    account_clause,
                )
                .order_by(SourceConnectionModel.id)
            )
            .scalars()
            .first()
        )
        return _to_domain(row) if row is not None else None

    def list_for_user(self, user_id: int) -> list[SourceConnection]:
        rows = (
            self._db.execute(
                select(SourceConnectionModel).where(SourceConnectionModel.user_id == user_id)
            )
            .scalars()
            .all()
        )
        return [_to_domain(r) for r in rows]

    def list_active(self) -> list[SourceConnection]:
        rows = (
            self._db.execute(
                select(SourceConnectionModel).where(
                    SourceConnectionModel.status == ConnectionStatus.ACTIVE.value
                )
            )
            .scalars()
            .all()
        )
        return [_to_domain(r) for r in rows]

    def update_status(
        self, connection_id: int, status: ConnectionStatus, last_error: str | None = None
    ) -> None:
        row = self._db.get(SourceConnectionModel, connection_id)
        if row is None:
            return
        row.status = status.value
        row.last_error = last_error
        self._db.flush()

    def update_cursor(self, connection_id: int, cursor: str, synced_at: datetime) -> None:
        row = self._db.get(SourceConnectionModel, connection_id)
        if row is None:
            return
        row.sync_cursor = cursor
        row.last_synced_at = synced_at
        self._db.flush()

    def update_display_name(self, connection_id: int, display_name: str) -> None:
        row = self._db.get(SourceConnectionModel, connection_id)
        if row is None:
            return
        row.display_name = display_name
        self._db.flush()


def _to_domain(row: SourceConnectionModel) -> SourceConnection:
    return SourceConnection(
        id=row.id,
        user_id=row.user_id,
        source_type=SourceType(row.source_type),
        external_account=row.external_account,
        display_name=row.display_name,
        status=ConnectionStatus(row.status),
        sync_cursor=row.sync_cursor,
        last_synced_at=row.last_synced_at,
        last_error=row.last_error,
        created_at=row.created_at,
    )
