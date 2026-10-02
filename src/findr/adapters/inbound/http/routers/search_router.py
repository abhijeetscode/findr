import math

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from findr.adapters.inbound.http.deps import get_search_index, get_workspace
from findr.adapters.outbound.elasticsearch.search_index_elasticsearch import ElasticsearchIndex
from findr.application.search.search_documents import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    SearchDocuments,
)
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
    if hit.source_type == SourceType.FILE:
        # Our own copy of the original (specs/open-files-and-pdf-pages.md §3.1).
        return f"/documents/{hit.document.id}/file"
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
    # Pages of a PDF the match is on; empty when unknown or not paged. Not
    # to be confused with SearchResponse.page, the page of *results*.
    pages: list[int]


class SearchResponse(BaseModel):
    """One page of results — see specs/search-pagination.md §4."""

    results: list[SearchHitResponse]
    page: int
    page_size: int
    # Results that can be paged through (at most 200).
    total: int
    total_pages: int
    # More matches exist than can be paged through.
    total_is_capped: bool


@router.get("", response_model=SearchResponse)
def search(
    q: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    workspace: Workspace = Depends(get_workspace),
    search_index: ElasticsearchIndex = Depends(get_search_index),
) -> SearchResponse:
    use_case = SearchDocuments(search_index)
    # A page past the end comes back empty with the real totals, not an
    # error: results can shrink between requests (spec §4).
    results = use_case.execute(workspace.user_id, workspace.id, q, page, page_size)
    return SearchResponse(
        page=page,
        page_size=page_size,
        total=results.total,
        total_pages=math.ceil(results.total / page_size),
        total_is_capped=results.total_is_capped,
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
                pages=hit.pages,
            )
            for hit in results.hits
        ]
    )
