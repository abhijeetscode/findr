from typing import Protocol

from findr.domain.entities import SearchHit


class SearchIndex(Protocol):
    """Kept separate from DocumentRepository so a vector/semantic-search adapter
    can be added later without touching domain or application code."""

    def search(self, user_id: int, query: str) -> list[SearchHit]: ...
