from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from findr.adapters.inbound.http.deps import get_search_index, get_workspace
from findr.adapters.outbound.elasticsearch.search_index_elasticsearch import ElasticsearchIndex
from findr.application.search.search_documents import SearchDocuments
from findr.domain.entities import SearchHit, Workspace
from findr.domain.value_objects import SourceType

# Search only ever covers one workspace (specs/workspaces.md §2).
router = APIRouter(prefix="/workspaces/{workspace_id}/search", tags=["search"])


def _source_url(hit: SearchHit) -> str | None:
    """Deep link back to the original item, per source's own URL scheme.
    Lives here (not in the connector or the frontend) because it's neither
    sync logic nor generic presentation — just "how does each source expose
    a link to one of its items," which is a fact about that source."""
    external_id = hit.document.external_id
    if hit.source_type == SourceType.GMAIL:
        return f"https://mail.google.com/mail/u/0/#all/{external_id}"
    return None


class SearchHitResponse(BaseModel):
    document_id: int
    source_type: str
    subject: str | None
    sender: str | None
    recipients: str | None
    snippet: str
    score: float
    sent_at: str | None
    url: str | None


class SearchResponse(BaseModel):
    results: list[SearchHitResponse]


@router.get("", response_model=SearchResponse)
def search(
    q: str = Query(..., min_length=1),
    workspace: Workspace = Depends(get_workspace),
    search_index: ElasticsearchIndex = Depends(get_search_index),
) -> SearchResponse:
    use_case = SearchDocuments(search_index)
    hits = use_case.execute(workspace.user_id, workspace.id, q)
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
                url=_source_url(hit),
            )
            for hit in hits
        ]
    )
