from findr.domain.entities import SourceConnection
from findr.ports.source_connection_repo import SourceConnectionRepository


class ListConnections:
    def __init__(self, connection_repo: SourceConnectionRepository) -> None:
        self._connection_repo = connection_repo

    def execute(self, workspace_id: int) -> list[SourceConnection]:
        """One workspace's connections. The caller must already have checked
        the workspace belongs to the user (GetWorkspace)."""
        return self._connection_repo.list_for_workspace(workspace_id)
