from typing import Protocol

from findr.domain.entities import Document


class DocumentRepository(Protocol):
    def upsert_many(self, documents: list[Document]) -> None: ...
    def delete_many(self, connection_id: int, external_ids: list[str]) -> None: ...
