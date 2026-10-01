from datetime import datetime
from typing import Protocol

from findr.domain.entities import SourceConnection
from findr.domain.value_objects import ConnectionStatus, SourceType


class SourceConnectionRepository(Protocol):
    def create(
        self,
        user_id: int,
        workspace_id: int,
        source_type: SourceType,
        external_account: str | None,
        display_name: str | None = None,
    ) -> SourceConnection: ...
    def get(self, connection_id: int, user_id: int) -> SourceConnection | None: ...
    def get_by_account(
        self, user_id: int, source_type: SourceType, external_account: str | None
    ) -> SourceConnection | None:
        """User-wide on purpose: an account (e.g. a Gmail address) can be
        connected in only one of the user's workspaces, and this finds it
        whichever workspace it's in (specs/workspaces.md §5.2).
        external_account=None matches a NULL column."""
        ...
    def get_in_workspace(
        self, workspace_id: int, source_type: SourceType, external_account: str | None
    ) -> SourceConnection | None:
        """Like get_by_account but within one workspace — used for each
        workspace's own "Uploaded files" connection."""
        ...
    def list_for_workspace(self, workspace_id: int) -> list[SourceConnection]: ...
    def delete(self, connection_id: int) -> None:
        """Removes the connection row, including its stored tokens. Its
        documents must already be gone (foreign key)."""
        ...
    def list_active(self) -> list[SourceConnection]:
        """All connections across all users with status ACTIVE — used by the
        background sync scheduler's tick, which isn't scoped to one user."""
        ...
    def update_status(
        self, connection_id: int, status: ConnectionStatus, last_error: str | None = None
    ) -> None: ...
    def update_cursor(self, connection_id: int, cursor: str, synced_at: datetime) -> None: ...
    def update_display_name(self, connection_id: int, display_name: str) -> None:
        """Refreshes the display label on reconnect (e.g. the account was
        renamed since the connection was first made)."""
        ...
