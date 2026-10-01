from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as DbSession

from findr.adapters.outbound.postgres.models import (
    DocumentChunkModel,
    DocumentModel,
    SourceConnectionModel,
)
from findr.adapters.outbound.system_clock import SystemClock
from findr.domain.entities import ChunkMetadata, Document, DocumentChunk
from findr.domain.value_objects import ChunkKind
from findr.ports.clock import Clock


class DocumentRepositoryPostgres:
    """Implements ports.document_repository.DocumentRepository.

    upsert_many returns the persisted documents with their real Postgres
    ids populated (via RETURNING) — required by specs/elasticsearch-search.md
    §2/§3.1: Elasticsearch's document _id is the Postgres documents.id, which
    doesn't exist until after this write, so SyncSource needs it back to
    index correctly (the id=0 placeholder every connector sets would
    otherwise collide every document in a batch onto the same ES _id).

    Also owns the document_chunks table (specs/upload-chunking.md §6.4): a
    document's chunks are replaced on upsert only when it has chunks, so
    sources that don't chunk (Gmail) never touch the table.
    """

    def __init__(self, db: DbSession, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()

    def upsert_many(self, documents: list[Document]) -> list[Document]:
        for doc in documents:
            stmt = pg_insert(DocumentModel).values(
                user_id=doc.user_id,
                connection_id=doc.connection_id,
                # Always the connection's workspace — never trusted from the
                # caller, so it can't disagree (specs/workspaces.md §3.2).
                workspace_id=(
                    select(SourceConnectionModel.workspace_id)
                    .where(SourceConnectionModel.id == doc.connection_id)
                    .scalar_subquery()
                ),
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
            ).returning(DocumentModel.id, DocumentModel.workspace_id)
            doc.id, doc.workspace_id = self._db.execute(stmt).one()
            if doc.chunks:
                self._replace_chunks(doc.id, doc.chunks)
        self._db.flush()
        return documents

    def delete_many(self, connection_id: int, external_ids: list[str]) -> None:
        if not external_ids:
            return
        doomed = select(DocumentModel.id).where(
            DocumentModel.connection_id == connection_id,
            DocumentModel.external_id.in_(external_ids),
        )
        self._db.execute(delete(DocumentChunkModel).where(DocumentChunkModel.document_id.in_(doomed)))
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
        document = _to_domain(row)
        document.chunks = load_chunks(self._db, [document_id]).get(document_id, [])
        return document

    def delete_by_id(self, document_id: int) -> None:
        self._db.execute(
            delete(DocumentChunkModel).where(DocumentChunkModel.document_id == document_id)
        )
        self._db.execute(delete(DocumentModel).where(DocumentModel.id == document_id))
        self._db.flush()

    def delete_for_workspace(self, workspace_id: int) -> None:
        doomed = select(DocumentModel.id).where(DocumentModel.workspace_id == workspace_id)
        self._db.execute(delete(DocumentChunkModel).where(DocumentChunkModel.document_id.in_(doomed)))
        self._db.execute(delete(DocumentModel).where(DocumentModel.workspace_id == workspace_id))
        self._db.flush()

    def _replace_chunks(self, document_id: int, chunks: list[DocumentChunk]) -> None:
        self._db.execute(
            delete(DocumentChunkModel).where(DocumentChunkModel.document_id == document_id)
        )
        self._db.add_all(
            DocumentChunkModel(
                document_id=document_id,
                chunk_index=chunk.metadata.chunk_index,
                kind=chunk.kind.value,
                text=chunk.text,
                table_html=chunk.table_html,
                document_version=chunk.metadata.document_version,
                content_sha256=chunk.metadata.content_sha256,
                workspace_id=chunk.metadata.workspace_id,
                metadata_json=_metadata_to_json(chunk.metadata),
            )
            for chunk in chunks
        )


def load_chunks(db: DbSession, document_ids: list[int]) -> dict[int, list[DocumentChunk]]:
    """Chunks for many documents in one query, each list in chunk order.
    Public so scripts/reindex_search.py can rebuild chunk vectors from
    Postgres without re-parsing any files."""
    if not document_ids:
        return {}
    rows = db.execute(
        select(DocumentChunkModel)
        .where(DocumentChunkModel.document_id.in_(document_ids))
        .order_by(DocumentChunkModel.document_id, DocumentChunkModel.chunk_index)
    ).scalars()
    by_document: dict[int, list[DocumentChunk]] = {}
    for row in rows:
        by_document.setdefault(row.document_id, []).append(_chunk_to_domain(row))
    return by_document


# document_version, content_sha256 and workspace_id are real columns;
# everything else in ChunkMetadata lives in metadata_json.
_JSON_FIELDS = (
    "chunk_index",
    "filename",
    "mime_type",
    "page_start",
    "page_end",
    "section_title",
    "element_types",
    "languages",
    "is_continuation",
    "parser_version",
)


def _metadata_to_json(metadata: ChunkMetadata) -> dict:
    return {name: getattr(metadata, name) for name in _JSON_FIELDS}


def _chunk_to_domain(row: DocumentChunkModel) -> DocumentChunk:
    data = row.metadata_json
    return DocumentChunk(
        text=row.text,
        kind=ChunkKind(row.kind),
        table_html=row.table_html,
        metadata=ChunkMetadata(
            chunk_index=row.chunk_index,
            document_version=row.document_version,
            content_sha256=row.content_sha256,
            workspace_id=row.workspace_id,
            filename=data["filename"],
            mime_type=data["mime_type"],
            page_start=data.get("page_start"),
            page_end=data.get("page_end"),
            section_title=data.get("section_title"),
            element_types=list(data.get("element_types", [])),
            languages=list(data.get("languages", [])),
            is_continuation=bool(data.get("is_continuation", False)),
            parser_version=data["parser_version"],
        ),
    )


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
        workspace_id=row.workspace_id,
    )
