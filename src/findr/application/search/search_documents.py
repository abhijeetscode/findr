import logging

from findr.domain.entities import SearchHit
from findr.observability import timed
from findr.ports.search_index import SearchIndex

logger = logging.getLogger(__name__)


class SearchDocuments:
    def __init__(self, search_index: SearchIndex) -> None:
        self._search_index = search_index

    def execute(self, user_id: int, workspace_id: int, query: str) -> list[SearchHit]:
        # The query's length only, never its text (specs/logging-telemetry.md §8).
        with timed(logger, "search.executed", query_chars=len(query)) as event:
            hits = self._search_index.search(user_id, workspace_id, query)
            event["hits"] = len(hits)
        return hits
