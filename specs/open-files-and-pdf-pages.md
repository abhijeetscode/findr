# Spec: Open uploaded files from search, and show PDF page numbers

Status: **Implemented** (branch `feature/pdf-open-page`). What changed during implementation is recorded in §8.
Owner: findr
Related: `specs/file-upload.md` (upload storage), `specs/upload-chunking.md` §6 (chunks carry `page_start`/`page_end`), `specs/elasticsearch-search.md` §4 (index mapping), `specs/semantic-search.md` §12 (hybrid BM25 + kNN search).

## 1. Purpose & scope

Two changes to search results for uploaded files:

1. **Open the file.** Clicking an uploaded-file result opens the original file in a new browser tab. PDFs, TXT and Markdown open in the browser; DOCX is downloaded (browsers can't render it). Today uploaded-file results aren't clickable at all.
2. **Page numbers.** A PDF result shows, as a badge next to the source badge, **every** page on which the search term was found, e.g. `p. 3–5, 12`.

Clicking opens the file at its start. It deliberately does **not** jump to a page (no `#page=N`).

### Out of scope
- Jumping to, or highlighting, the matched page inside the viewer.
- Page numbers for DOCX/TXT/Markdown. They have no page metadata.
- Page numbers for Gmail results.
- An in-app viewer. We rely on the browser's own PDF viewer.

## 2. Behaviour

### 2.1 Which pages are shown
- **Keyword matches.** A page is listed if one chunk on it contains **every** word of the query. Matching uses the same analyzer as the `body_text` keyword search. The rule is AND, not the OR that ranks documents: with OR, a common word such as "the" in "the renewal" would list nearly every page. A natural-language query with no chunk containing all its words falls through to the semantic pages below. A one-word query behaves the same under either rule.
- **Semantic-only matches** (the kNN leg found the document but BM25 didn't): show the pages of the single best-matching chunk, which the kNN leg already returns. Otherwise a semantic hit would have no badge at all.
- **Chunks spanning pages.** A chunk with `page_start=3, page_end=4` contributes pages 3 and 4. Chunk overlap (150 chars) can occasionally add a neighbouring page. That's acceptable.
- Pages are de-duplicated and sorted ascending.
- **No badge** when no pages are known. That covers non-PDF files, a match only in the filename (subject), and uploads indexed before this change until the backfill runs (§5).
- **Cap.** Elasticsearch returns at most 100 matching chunks per document (`index.max_inner_result_window`). If more chunks match, the list may be incomplete. That's acceptable at our document sizes.

### 2.2 Display
- The badge goes next to the source badge in `.result-meta-row`.
- Consecutive pages are compressed into ranges: `[3,4,5,12]` → `p. 3–5, 12`. One page reads `p. 7`. The list is never truncated.

### 2.3 Opening the file
- `SearchHitResponse.url` is set for `source_type == "file"` to `/documents/{document_id}/file`. The card becomes an `<a target="_blank">`, the same as Gmail results today.
- The UI and API share an origin, so the session cookie is sent with the new tab's request.
- **Delete button gotcha.** The existing Delete button sits inside the card. Once the card is a link, clicking Delete would also open the file. Its handler must call `preventDefault()` and `stopPropagation()`.

## 3. API

### 3.1 `GET /documents/{document_id}/file` (new)
- Requires login (`get_current_user`).
- Returns the original bytes.
- **404** for every failure, with the same body whether the document doesn't exist, belongs to another user, or isn't an uploaded file. This matches `DELETE /documents/{id}`.

Response headers:

| Header | Value |
|---|---|
| `Content-Type` | PDF: `application/pdf`. DOCX: its stored MIME type. TXT and Markdown: **`text/plain; charset=utf-8`**, never `text/html` or `text/markdown`. A Markdown file containing `<script>` must not run in our origin. |
| `Content-Disposition` | `inline; filename="<ascii fallback>"; filename*=UTF-8''<percent-encoded original name>` |
| `X-Content-Type-Options` | `nosniff` |
| `Cache-Control` | `private, no-store` |

### 3.2 `GET /workspaces/{id}/search` (changed)
`SearchHitResponse` gains:
- `pages: list[int]` (sorted, empty when unknown).
- `url` is now set for file hits (§2.3).

## 4. Design (ports & adapters)

| Layer | Change |
|---|---|
| Domain `SearchHit` | Add `pages: list[int] = field(default_factory=list)`. |
| Port `SearchIndex.search` | Docstring: for paged sources, a hit's `pages` lists the pages its matching content is on. No signature change. |
| Application | New use case `GetDocumentFile(uploaded_files: UploadedFileRepository, storage: FileStorage)`. It loads the upload by `get_by_document_id`, checks `upload.user_id == user_id`, and returns `(bytes, mime_type, original_filename)`. It raises `DocumentNotFound` otherwise. |
| Adapter: ES mapping (`es_client.py`) | `chunks.text` gets a multi-field `fields: {"search": {"type": "text"}}`. The parent stays `index: false`. It is added to existing indices by the additive `put_mapping` in `ensure_index`; this was verified to be accepted on a live index. |
| Adapter: ES search | The BM25 leg gains a **non-scoring** `should` clause: a `nested` query on `chunks` matching `chunks.text.search`, wrapped in `constant_score` with `boost: 0`. Its `inner_hits` (size 100) returns only `chunks.metadata.page_start/page_end`. It sits under `should`, not `filter` or `must`, so it **cannot change which documents match or how they rank**. Pages are read from the BM25 leg's inner hits, falling back to the kNN leg's best chunk (§2.1). The kNN `inner_hits` `_source` adds the page fields. |
| Adapter: HTTP | `documents_router.py` gains the route (§3.1). `search_router._source_url` handles `SourceType.FILE`. `SearchHitResponse.pages` is added. |
| UI | Page badge (§2.2) and the Delete button fix (§2.3). |

**Rejected alternative:** compute pages at request time by loading `document_chunks` from Postgres and substring-matching in Python. It needs no reindex, but it duplicates Elasticsearch's analyzer (case, tokenization) in Python, so its notion of "match" would drift from the search's. It would also add a DB round-trip to every search.

## 5. Rollout / backfill
- A new index gets the field on creation. An existing index gets the mapping at app startup (`ensure_index`). Documents already indexed only become page-searchable once re-indexed.
- **Cheap backfill (no re-embedding):** after the app has started once on the new code (so `ensure_index` has added the mapping), run:

  ```sh
  curl -X POST "localhost:9200/<FINDR_ELASTICSEARCH_INDEX>/_update_by_query?conflicts=proceed&refresh=true"
  ```

  This re-indexes each document from its stored `_source`, which populates the new sub-field. Verified on a throwaway index: old chunks became searchable and kept their embeddings. `scripts/reindex_search.py` also works, but re-embeds every chunk.
- Until the backfill runs, old PDF hits fall back to the semantic chunk's pages, or show no badge.

## 6. Tests
- **`GetDocumentFile`:** returns bytes for the owner; raises `DocumentNotFound` for another user's document, a missing document, and a Gmail document.
- **Router `/documents/{id}/file`:**
  - 401 when logged out.
  - Cross-user 404.
  - PDF headers.
  - `.md` is served as `text/plain; charset=utf-8` with `nosniff`.
  - A non-ASCII filename produces a valid `Content-Disposition`.
- **ES adapter (against the test ES):**
  - Pages are collected from the matching chunks only.
  - A multi-page chunk expands to all its pages.
  - Results are de-duplicated and sorted.
  - **Ranking and match set are identical** with and without the new clause.
  - A filename-only match has no pages.
  - A kNN-only hit gets its best chunk's pages.
- **Search router:** `pages` and the file `url` appear in the response.
- **UI range formatting:** `[3,4,5,12]` → `3–5, 12` (manual check).

## 7. Open questions
None. Decisions agreed with the owner:
- Show all pages.
- No jump to a page on open.
- Other file types open too.
- Badge next to the source badge.

## 8. Implementation notes
- **Files changed:**
  - `domain/entities.py`: `SearchHit.pages`.
  - `ports/search_index.py`: docstring.
  - `application/uploads/get_document_file.py`: new.
  - `es_client.py`: the `chunks.text.search` multi-field.
  - `search_index_elasticsearch.py`: the BM25 `should` clause named `keyword_pages`, and page fields on the kNN inner hits.
  - `documents_router.py`: `GET /{id}/file`.
  - `search_router.py`: `url` and `pages`.
  - `Unified Search Interface.html`: the badge, `formatPages`, and the Delete click fix.
- **Not changed:** `Unified Search Interface.bundle.html` is an unserved work-in-progress copy.
- **Filename matches:** Elasticsearch's standard tokenizer keeps `renewal.pdf` as one token, so a query for "renewal" doesn't match that filename. This was already the case and isn't changed here. It's noted because the filename-only test uses `renewal report.pdf`.
- **Changed from the draft:** keyword pages use AND, not OR (§2.1). This was found while testing multi-word queries.
- **Missing original:** a file missing on disk while its upload row still exists surfaces as a 500, not a 404. This shouldn't happen, because deletion removes the row first.
