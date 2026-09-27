from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session as DbSession

from findr.domain.entities import Document, SearchHit
from findr.domain.value_objects import SourceType

_TOKEN_RE = re.compile(r"\S+")

_SEARCH_SQL = text(
    """
    SELECT d.id, d.user_id, d.connection_id, d.external_id,
           d.subject, d.sender, d.recipients, d.body_text, d.sent_at,
           sc.source_type AS source_type,
           snippet(documents_fts, 2, '[', ']', '…', 10) AS snippet,
           bm25(documents_fts) AS rank
    FROM documents_fts
    JOIN documents d ON d.id = documents_fts.rowid
    JOIN source_connections sc ON sc.id = d.connection_id
    WHERE documents_fts MATCH :query AND d.user_id = :user_id
    ORDER BY rank
    LIMIT 50
    """
)


def _sanitize_fts_query(raw_query: str) -> str:
    """Wrap each whitespace-separated token in double quotes so FTS5 special
    characters in user input (", -, :, *, ...) can't be read as MATCH query
    syntax (column filters, prefix search, NOT, ...) — just literal terms."""
    tokens = _TOKEN_RE.findall(raw_query)
    escaped = ['"' + token.replace('"', '""') + '"' for token in tokens]
    return " ".join(escaped)


class SearchIndexSqlite:
    """Implements ports.search_index.SearchIndex, via SQLite FTS5.

    Kept as its own adapter (not folded into DocumentRepositorySqlite) so a
    future vector-search adapter can implement the same port without this
    class changing.
    """

    def __init__(self, db: DbSession) -> None:
        self._db = db

    def search(self, user_id: int, query: str) -> list[SearchHit]:
        fts_query = _sanitize_fts_query(query)
        if not fts_query:
            return []

        rows = self._db.execute(_SEARCH_SQL, {"query": fts_query, "user_id": user_id}).all()

        hits = []
        for row in rows:
            document = Document(
                id=row.id,
                user_id=row.user_id,
                connection_id=row.connection_id,
                external_id=row.external_id,
                subject=row.subject,
                sender=row.sender,
                recipients=row.recipients,
                body_text=row.body_text,
                # A raw text() query bypasses SQLAlchemy's DateTime result
                # processor, so the driver hands back the stored string
                # as-is rather than a datetime — parse it explicitly.
                sent_at=datetime.fromisoformat(row.sent_at) if row.sent_at else None,
            )
            # bm25() is more-negative-is-better; flip sign so a higher
            # SearchHit.score means more relevant.
            hits.append(
                SearchHit(
                    document=document,
                    snippet=row.snippet,
                    score=-row.rank,
                    source_type=SourceType(row.source_type),
                )
            )
        return hits
