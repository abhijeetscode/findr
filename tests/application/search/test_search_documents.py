"""specs/search-pagination.md §5.3: turning a page number into a slice."""

import logging

import pytest

from findr.application.search.search_documents import MAX_RESULTS, SearchDocuments
from findr.domain.entities import SearchResults


class RecordingSearchIndex:
    def __init__(self, total=137, capped=False) -> None:
        self.calls: list[dict] = []
        self._total = total
        self._capped = capped

    def search(self, user_id, workspace_id, query, *, offset, limit, max_results):
        self.calls.append(dict(offset=offset, limit=limit, max_results=max_results))
        return SearchResults(hits=[], total=self._total, total_is_capped=self._capped)


@pytest.mark.parametrize(
    ("page", "page_size", "offset", "limit"),
    [
        (1, 20, 0, 20),
        (2, 20, 20, 20),
        (10, 20, 180, 20),
        # Past the limit: no slice, but the totals still come back.
        (11, 20, 200, 0),
        # A page straddling the limit is cut at it.
        (7, 30, 180, 20),
    ],
)
def test_page_becomes_an_offset_and_limit_within_the_search_limit(page, page_size, offset, limit):
    index = RecordingSearchIndex()

    results = SearchDocuments(index).execute(1, 2, "renewal", page, page_size)

    assert index.calls == [dict(offset=offset, limit=limit, max_results=MAX_RESULTS)]
    assert results.total == 137


def test_defaults_to_the_first_page_of_twenty():
    index = RecordingSearchIndex()
    SearchDocuments(index).execute(1, 2, "renewal")
    assert index.calls == [dict(offset=0, limit=20, max_results=200)]


@pytest.mark.parametrize(("page", "page_size"), [(0, 20), (1, 0), (1, 51)])
def test_rejects_invalid_pages(page, page_size):
    with pytest.raises(ValueError):
        SearchDocuments(RecordingSearchIndex()).execute(1, 2, "renewal", page, page_size)


def test_logs_the_page_and_totals(caplog):
    caplog.set_level(logging.INFO)
    SearchDocuments(RecordingSearchIndex(total=200, capped=True)).execute(1, 2, "renewal", 3)

    [event] = [r for r in caplog.records if getattr(r, "event", None) == "search.executed"]
    assert (event.page, event.page_size, event.hits) == (3, 20, 0)
    assert (event.total, event.total_is_capped) == (200, True)
