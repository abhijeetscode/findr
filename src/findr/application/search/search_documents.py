from findr.domain.entities import SearchHit
from findr.ports.search_index import SearchIndex


class SearchDocuments:
    def __init__(self, search_index: SearchIndex) -> None:
        self._search_index = search_index

    def execute(self, user_id: int, workspace_id: int, query: str) -> list[SearchHit]:
        return self._search_index.search(user_id, workspace_id, query)
