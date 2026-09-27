from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from findr.adapters.inbound.http.deps import get_current_user, get_db_session
from findr.adapters.outbound.sqlite.search_index_sqlite import SearchIndexSqlite
from findr.application.search.search_documents import SearchDocuments
from findr.domain.entities import User

router = APIRouter(prefix="/search", tags=["search"])


class SearchHitResponse(BaseModel):
    document_id: int
    source_type: str
    subject: str | None
    sender: str | None
    recipients: str | None
    snippet: str
    score: float
    sent_at: str | None


class SearchResponse(BaseModel):
    results: list[SearchHitResponse]


@router.get("", response_model=SearchResponse)
def search(
    q: str = Query(..., min_length=1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
) -> SearchResponse:
    use_case = SearchDocuments(SearchIndexSqlite(db))
    hits = use_case.execute(user.id, q)
    return SearchResponse(
        results=[
            SearchHitResponse(
                document_id=hit.document.id,
                source_type=hit.source_type.value,
                subject=hit.document.subject,
                sender=hit.document.sender,
                recipients=hit.document.recipients,
                snippet=hit.snippet,
                score=hit.score,
                sent_at=hit.document.sent_at.isoformat() if hit.document.sent_at else None,
            )
            for hit in hits
        ]
    )
