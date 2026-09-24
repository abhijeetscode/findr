from findr.domain.entities import SourceConnection
from findr.ports.source_connection_repo import SourceConnectionRepository


class ListConnections:
    def __init__(self, connection_repo: SourceConnectionRepository) -> None:
        self._connection_repo = connection_repo

    def execute(self, user_id: int) -> list[SourceConnection]:
        return self._connection_repo.list_for_user(user_id)
