import logging

from findr.domain.entities import SearchResults
from findr.observability import timed
from findr.ports.search_index import SearchIndex

logger = logging.getLogger(__name__)

# How deep search goes, and results per page (specs/search-pagination.md §1).
MAX_RESULTS = 200
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50


class SearchDocuments:
    def __init__(self, search_index: SearchIndex) -> None:
        self._search_index = search_index

    def execute(
        self,
        user_id: int,
        workspace_id: int,
        query: str,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> SearchResults:
        """One page of results. A page past the end is empty but still
        carries the real totals, so a caller can find the last page."""
        if page < 1 or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"Invalid page {page} / page size {page_size}")
        offset = (page - 1) * page_size
        # The query's length only, never its text (specs/logging-telemetry.md §8).
        with timed(
            logger, "search.executed", query_chars=len(query), page=page, page_size=page_size
        ) as event:
            results = self._search_index.search(
                user_id,
                workspace_id,
                query,
                offset=offset,
                # Never past the limit, even mid-page (e.g. 190 + 20).
                limit=max(0, min(page_size, MAX_RESULTS - offset)),
                max_results=MAX_RESULTS,
            )
            event.update(
                hits=len(results.hits),
                total=results.total,
                total_is_capped=results.total_is_capped,
            )
        return results
