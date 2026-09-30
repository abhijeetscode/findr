from typing import Protocol

from findr.domain.entities import Document


class DocumentRepository(Protocol):
    def upsert_many(self, documents: list[Document]) -> list[Document]:
        """Returns the same documents with `.id` populated with whatever the
        store assigned them — needed by SyncSource to index into
        Elasticsearch under the right id (see specs/elasticsearch-search.md
        §2/§3.1)."""
        ...
    def delete_many(self, connection_id: int, external_ids: list[str]) -> None: ...
    def get(self, document_id: int, user_id: int) -> Document | None:
        """None both when the document doesn't exist and when it belongs to
        a different user — callers must not be able to tell the two apart."""
        ...
    def delete_by_id(self, document_id: int) -> None: ...
