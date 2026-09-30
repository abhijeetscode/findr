from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as DbSession

from findr.adapters.outbound.postgres.models import DocumentModel
from findr.adapters.outbound.system_clock import SystemClock
from findr.domain.entities import Document
from findr.ports.clock import Clock


class DocumentRepositoryPostgres:
    """Implements ports.document_repository.DocumentRepository.

    upsert_many returns the persisted documents with their real Postgres
    ids populated (via RETURNING) — required by specs/elasticsearch-search.md
    §2/§3.1: Elasticsearch's document _id is the Postgres documents.id, which
    doesn't exist until after this write, so SyncSource needs it back to
    index correctly (the id=0 placeholder every connector sets would
    otherwise collide every document in a batch onto the same ES _id).
    """

    def __init__(self, db: DbSession, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()

    def upsert_many(self, documents: list[Document]) -> list[Document]:
        for doc in documents:
            stmt = pg_insert(DocumentModel).values(
                user_id=doc.user_id,
                connection_id=doc.connection_id,
                external_id=doc.external_id,
                subject=doc.subject,
                sender=doc.sender,
                recipients=doc.recipients,
                body_text=doc.body_text,
                sent_at=doc.sent_at,
                thread_id=doc.thread_id,
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
            ).returning(DocumentModel.id)
            doc.id = self._db.execute(stmt).scalar_one()
        self._db.flush()
        return documents

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

    def get(self, document_id: int, user_id: int) -> Document | None:
        row = self._db.get(DocumentModel, document_id)
        if row is None or row.user_id != user_id:
            return None
        return _to_domain(row)

    def delete_by_id(self, document_id: int) -> None:
        self._db.execute(delete(DocumentModel).where(DocumentModel.id == document_id))
        self._db.flush()


def _to_domain(row: DocumentModel) -> Document:
    return Document(
        id=row.id,
        user_id=row.user_id,
        connection_id=row.connection_id,
        external_id=row.external_id,
        subject=row.subject,
        sender=row.sender,
        recipients=row.recipients,
        body_text=row.body_text,
        sent_at=row.sent_at,
        thread_id=row.thread_id,
    )
