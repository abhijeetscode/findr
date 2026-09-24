from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session as DbSession

from findr.adapters.outbound.sqlite.models import DocumentModel
from findr.adapters.outbound.system_clock import SystemClock
from findr.domain.entities import Document
from findr.ports.clock import Clock


class DocumentRepositorySqlite:
    """Implements ports.document_repository.DocumentRepository.

    Upserts use INSERT ... ON CONFLICT DO UPDATE (never INSERT OR REPLACE)
    so the documents_fts sync triggers in schema.sql fire correctly.
    """

    def __init__(self, db: DbSession, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()

    def upsert_many(self, documents: list[Document]) -> None:
        for doc in documents:
            stmt = sqlite_insert(DocumentModel).values(
                user_id=doc.user_id,
                connection_id=doc.connection_id,
                external_id=doc.external_id,
                subject=doc.subject,
                sender=doc.sender,
                recipients=doc.recipients,
                body_text=doc.body_text,
                sent_at=doc.sent_at,
                created_at=self._clock.now(),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[DocumentModel.connection_id, DocumentModel.external_id],
                set_={
                    "subject": stmt.excluded.subject,
                    "sender": stmt.excluded.sender,
                    "recipients": stmt.excluded.recipients,
                    "body_text": stmt.excluded.body_text,
                    "sent_at": stmt.excluded.sent_at,
                },
            )
            self._db.execute(stmt)
        self._db.flush()

    def delete_many(self, connection_id: int, external_ids: list[str]) -> None:
        if not external_ids:
            return
        self._db.execute(
            delete(DocumentModel).where(
                DocumentModel.connection_id == connection_id,
                DocumentModel.external_id.in_(external_ids),
            )
        )
        self._db.flush()
