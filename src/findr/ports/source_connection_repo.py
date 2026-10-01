from datetime import datetime
from typing import Protocol

from findr.domain.entities import SourceConnection
from findr.domain.value_objects import ConnectionStatus, SourceType


class SourceConnectionRepository(Protocol):
    def create(
        self,
        user_id: int,
        source_type: SourceType,
        external_account: str | None,
        display_name: str | None = None,
    ) -> SourceConnection: ...
    def get(self, connection_id: int, user_id: int) -> SourceConnection | None: ...
    def get_by_account(
        self, user_id: int, source_type: SourceType, external_account: str | None
    ) -> SourceConnection | None:
        """external_account=None matches a NULL column (the per-user FILE
        "Uploaded files" connection has no external account)."""
        ...
    def list_for_user(self, user_id: int) -> list[SourceConnection]: ...
    def list_active(self) -> list[SourceConnection]:
        """All connections across all users with status ACTIVE — used by the
        background sync scheduler's tick, which isn't scoped to one user."""
        ...
    def update_status(
        self, connection_id: int, status: ConnectionStatus, last_error: str | None = None
    ) -> None: ...
    def update_cursor(self, connection_id: int, cursor: str, synced_at: datetime) -> None: ...
    def update_display_name(self, connection_id: int, display_name: str) -> None:
        """Refreshes the display label on reconnect (e.g. a Slack workspace
        or Notion page was renamed since the connection was first made)."""
        ...
