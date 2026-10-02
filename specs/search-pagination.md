# Spec: Paginated search results

Status: **Implemented** (branch `feature/search-pagination`). Decisions in §10; implementation notes in §11.
Owner: findr
Related: `specs/semantic-search.md` (hybrid BM25 + kNN with reciprocal rank fusion, which shapes how paging works, §3), `specs/open-files-and-pdf-pages.md` (adds `pages` to each hit: the PDF pages a match is on, a different meaning of "page", §5.3), `specs/workspaces.md` (search is per workspace), `specs/logging-telemetry.md` (the `search.executed` event).

## 1. Purpose & scope

Today a search returns the top 50 results in one response and the UI shows them all. This change serves results **one page at a time**: 20 per page, up to 200 results in total, with numbered page links in the UI.

Decisions agreed before this spec:

| Question | Decision |
|---|---|
| How the UI moves through results | **Numbered pages**: `‹ Prev  1 [2] 3 4 … 10  Next ›` under the results, and a summary like "Showing 21–40 of 137 results". |
| Page size | **20** results per page. |
| How deep search goes | **Up to 200** results in total (today: 50). |

### In scope
- `GET /workspaces/{workspace_id}/search` takes `page` and returns that page plus the totals the UI needs.
- The `SearchIndex` port and `SearchDocuments` use case work in pages.
- The Elasticsearch adapter ranks up to 200 candidates once per request, then fetches details (highlights, snippets, PDF pages) for just the requested page's results (§3).
- The frontend pager, summary line and page-change behaviour (§6).

### Out of scope
- Infinite scroll or "Load more".
- A user-selectable page size. `page_size` is accepted by the API (§4), but the UI always sends 20.
- Keeping the page in the browser URL, so reloading or sharing a link lands on page 3. A possible follow-up (§9).
- Sorting by anything other than relevance, and filters.
- Freezing results between page requests. Each page re-runs the search, so an email synced or a file uploaded in between can shift results by a position or two (§7).
- More than 200 results.

## 2. Why paging hybrid search needs care

Search runs two queries, BM25 keyword search and kNN semantic search, and merges their rankings with **reciprocal rank fusion** (RRF) in Python (`_reciprocal_rank_fusion`). RRF scores a document by its position in each list, so the merged order is only correct if each list is fetched from the top.

Elasticsearch's own `from`/`size` paging can't be used per query: page 2 of the keyword list and page 2 of the semantic list don't fuse into page 2 of the merged list. So every page request:

1. fetches the **top 200** of each query,
2. fuses them into one ranked list of up to 200 documents (the same list for every page, given the same data), and
3. returns the slice for the requested page.

That's also why there's a total limit: the window being fused is the limit.

## 3. Two-phase query in the adapter

Fetching 200 results per query with highlights, inner hits (PDF pages, best semantic chunk) and full `_source` would make every page roughly 8× heavier than today's 50, and 190 of those results would be thrown away. So the adapter splits the work:

**Phase 1, ranking (cheap).** The same BM25 and kNN queries, `size`/`k` = 200, but with `_source: false`, no `highlight` and no `inner_hits`. Only ids and ranks come back. Fuse with RRF, giving the ordered id list, its length (`total`) and the page's slice of ids.

**Phase 2, details for the page (≤ 20 documents).** Today's two query bodies, unchanged except for an extra filter `{"ids": {"values": page_ids}}` on both, and `size`/`k` = the page length. This gets highlights, the PDF-page inner hits and the best semantic chunk exactly as today. The results are put back in phase 1's order, and each hit's `score` is its RRF score from phase 1.

- The query is embedded **once** per request and reused by both phases.
- Both phases go through one `msearch` each, so a page costs two round trips to Elasticsearch, up from one.
- A document can match in phase 1 but come back from neither phase-2 query (e.g. deleted in between). It's dropped from the page rather than failing the request, so a page can very occasionally have 19 results.
- An empty page (past the end) skips phase 2.

The kNN `similarity` floor and the workspace/user filters apply in both phases, unchanged.

`num_candidates` (the kNN shortlist each shard considers) must be at least `k`. It's 200 today with `k` = 50. Phase 1 raises it to 400, twice `k`, so asking for 200 neighbours doesn't make the approximate search miss good ones. Phase 2 filters to ≤ 20 ids, so its shortlist stays small.

## 4. API

`GET /workspaces/{workspace_id}/search?q=…&page=1&page_size=20`

| Parameter | Type | Default | Rules |
|---|---|---|---|
| `q` | string | required | Non-empty (unchanged). |
| `page` | int | `1` | ≥ 1, else 422. |
| `page_size` | int | `20` | 1–50, else 422. |

Response:

```json
{
  "results": [ /* SearchHitResponse, unchanged — including its "pages" field */ ],
  "page": 2,
  "page_size": 20,
  "total": 137,
  "total_pages": 7,
  "total_is_capped": false
}
```

- `total`: how many results can be paged through, at most 200.
- `total_pages`: `ceil(total / page_size)`, `0` when there are no results.
- `total_is_capped`: `true` when there are more matches than the 200 that can be shown. The UI then says "200+" (§6). It's `true` when the keyword query's own match count (`hits.total`) is over 200, or when the semantic query returned a full 200.
- **A page past the end** (e.g. `page=9` when `total_pages` is 7) returns `200` with empty `results` and the real totals, not an error. The UI uses the totals to recover (§6). Results can shrink between requests, so this is an expected case, not a client bug.
- `results` keeps its current shape, so a client that only reads `results` keeps working and gets the first 20.

## 5. Domain, ports and application

### 5.1 Domain
A new value object in `domain/entities.py`:

```python
@dataclass
class SearchResults:
    hits: list[SearchHit]   # this page, in rank order
    total: int              # results that can be paged through (≤ the limit)
    total_is_capped: bool   # more matches exist beyond the limit
```

### 5.2 Port
```python
class SearchIndex(Protocol):
    def search(
        self, user_id: int, workspace_id: int, query: str, *, offset: int, limit: int, max_results: int
    ) -> SearchResults: ...
```
`max_results` is the fusion window (§2). `offset` + `limit` select the slice. Passing the window in, rather than fixing it in the adapter, keeps "how deep search goes" a product rule in the application layer and leaves the adapter mechanical.

### 5.3 Use case
`SearchDocuments.execute(user_id, workspace_id, query, page, page_size) -> SearchResults`:
- `MAX_RESULTS = 200` and `DEFAULT_PAGE_SIZE = 20` live here.
- `offset = (page - 1) * page_size`. When `offset ≥ MAX_RESULTS`, it returns an empty page with the totals, still running phase 1 so the totals are real.
- `page`/`page_size` bounds are validated at the HTTP layer (FastAPI `Query`), and the use case asserts them too.
- The `search.executed` log event gains `page`, `page_size`, `total` and `total_is_capped`. `hits` becomes the number on this page.

Naming: the response's `page` (which page of results) and each hit's existing `pages` (PDF page numbers the match is on, `specs/open-files-and-pdf-pages.md`) are different things. The names stay as they are, both already established; the API docs and the code comments say which is which.

## 6. Frontend (`Unified Search Interface.html`)

- **Summary line:** "Showing 21–40 of 137 results". When `total_is_capped`: "Showing 21–40 of 200+ results". One page only: "12 results". No results: unchanged ("No results"). The current "… across N sources" part is dropped: it was counted from the visible results, and with paging it would describe only one page.
- **Pager** below the list, only when `total_pages > 1`: `‹ Prev`, page numbers, `Next ›`. Numbers shown are the first, the last, and the current page ±2, with `…` for gaps, e.g. `1 … 4 5 [6] 7 8 … 10`. Prev is disabled on page 1, Next on the last page. The current page is highlighted and not clickable.
- **Behaviour:**
  - Typing a new query, or switching workspace, goes back to page 1.
  - Changing page re-runs the search for that page and scrolls the results back to the top.
  - A late response for an older query or page is ignored, using the existing `stillCurrent` guard extended with the page number. This keeps clicking quickly through pages from showing stale results.
  - A page past the end (empty `results` with `total_pages > 0`) jumps once to the last page.
- Buttons are real `<button>`s, keyboard-focusable, with `aria-current="page"` on the current page and `aria-label`s on Prev/Next.
- Styling follows the existing pills/buttons in the page; no new dependencies.

## 7. Edge cases

| Case | Behaviour |
|---|---|
| No matches | `total: 0`, `total_pages: 0`, empty `results`; UI "No results", no pager. |
| Exactly 20 results | One page; no pager. |
| `page` past the end | 200, empty `results`, real totals; UI jumps to the last page (§6). |
| More than 200 matches | Only the top 200 reachable; `total_is_capped: true`; UI "200+". |
| Data changes between page requests | A result can move by a position or two, or appear on two pages or none. Accepted (out of scope §1); results are still correctly ranked within each request. |
| Document deleted between phase 1 and phase 2 | Dropped from that page (§3). |
| Query of only stopwords / special characters | Unchanged from today (no error; possibly no results). |
| Another user's or workspace's documents | Never appear: both phases carry the workspace and user filters. |

## 8. Testing

**Adapter (real Elasticsearch, existing fixtures):**
- With more documents than one page, consecutive pages are disjoint, in order, and together equal the unpaged ranking (`offset=0, limit=max_results`).
- `total` counts all fused results; `total_is_capped` is false below the window and true above it (a small `max_results`, e.g. 5, keeps the test fast).
- Phase 2 keeps today's details: highlight snippets, semantic-chunk snippets and PDF `pages` are the same as an unpaged search for the same documents (the existing snippet and page tests run through the paged path).
- An offset past the end returns no hits and the right totals.
- Workspace and user isolation hold on page 2 as well.

**Use case:** offset computed from `page`/`page_size`; past-the-limit pages are empty with totals; the log event carries the new fields.

**HTTP (TestClient):** defaults (`page=1`, `page_size=20`); 422 for `page=0`, `page_size=0` and `page_size=51`; response fields and `total_pages` arithmetic; a client reading only `results` gets the first page.

**Frontend:** manual check in the browser: pager rendering at 1, 2, 7 and 10+ pages, Prev/Next disabled states, reset on new query and workspace switch, rapid page clicks, "200+".

## 9. Follow-ups (not in this spec)
- Page number in the URL (`?q=…&page=3`), so reload, back/forward and shared links keep the page.
- Caching phase 1's ranked id list for a few seconds per (workspace, query), so page changes skip the ranking queries. Only worth it if page changes turn out slow.
- A user-selectable page size.

## 10. Decisions

| # | Question | Decision |
|---|---|---|
| Q1 | Summary when there are more than 200 matches | "of 200+". Not the keyword-only match count, which ignores semantic matches and could mislead. |
| Q2 | The "across N sources" part of the summary | Dropped. With paging it would only describe the visible page. |

## 11. Implementation notes

- `SearchDocuments.execute(user_id, workspace_id, query, page=1, page_size=20)` returns `SearchResults`. It caps the slice at the limit, so a page straddling 200 (e.g. `page_size=30`, page 7) gets 20 results and a page past 200 gets none. It raises `ValueError` on invalid input; the HTTP layer rejects that first with 422.
- The adapter's two query bodies are built by `_keyword_body` / `_semantic_body` with a `details` switch: ranking-only for phase 1, today's full bodies (page clause, highlight, best-chunk inner hits) for phase 2, which also gets an `ids` filter. The DEBUG `search_index.searched` event now reports `ranking_ms`, `details_ms` and `fused`.
- `total_is_capped` is true when the keyword query's `hits.total` exceeds the window (or is reported as a lower bound), or the semantic query returned a full window.
- Frontend: a `searchSeq` counter works alongside the existing workspace guard. Every request and every clear bumps it, and only the latest request's response renders. `refreshSearch` (after deleting a file) stays on the current page.
- Existing adapter tests now read `.hits` from the result. The page-clause test inspects the phase-2 (details) BM25 body, where that clause now lives.
