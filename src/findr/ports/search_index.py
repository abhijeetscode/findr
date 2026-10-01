from typing import Protocol

from findr.domain.entities import Document, SearchHit
from findr.domain.value_objects import SourceType


class SearchIndex(Protocol):
    """Kept separate from DocumentRepository so a vector/semantic-search adapter
    can be added later without touching domain or application code."""

    def search(self, user_id: int, workspace_id: int, query: str) -> list[SearchHit]:
        """Only ever returns documents from this one workspace (and user) —
        see specs/workspaces.md §2."""
        ...

    def index_documents(
        self,
        documents: list[Document],
        source_type: SourceType,
        external_account: str | None,
        workspace_id: int,
    ) -> None:
        """Mirrors DocumentRepository.upsert_many's shape — see
        specs/elasticsearch-search.md §2/§3.1. Called with upsert_many's
        *return value* (real ids populated), never the original list.

        Takes source_type/external_account in addition to the documents —
        a deviation from the spec's original text: Document itself doesn't
        carry either (they're connection-level facts), and unlike the old
        SQLite adapter, an index-based SearchIndex has no JOIN to a separate
        source_connections table, so they must be denormalized onto each
        indexed document at write time. The caller (SyncSource) already has
        the connection in scope."""
        ...

    def delete_documents(self, connection_id: int, external_ids: list[str]) -> None:
        """Mirrors DocumentRepository.delete_many's exact inputs — the
        caller never needs to know an index-internal id to delete
        something."""
        ...

    def delete_workspace(self, workspace_id: int) -> None:
        """Removes every indexed document of a deleted workspace."""
        ...
