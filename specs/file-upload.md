# Spec: File upload as a source

Status: **Implemented** (branch `feature/file-upload-semantic-search`). Deviations from the draft are recorded at the end of this spec.
Owner: findr
Related: `CLAUDE.md` ("MVP scope: uploaded files + Gmail" — this spec is the "uploaded files" half, the last piece of the originally-scoped MVP), `specs/semantic-search.md` (sibling spec — after its §12 revision, uploaded documents are the *only* documents that get embedded and searched semantically (hybrid keyword + semantic); Gmail stays keyword-only. Implemented together on one branch, same pattern as `postgres-migration.md`/`elasticsearch-search.md`), `specs/gmail-connector.md` (established the `SourceConnector`/`SourceConnection` shape this spec deliberately does *not* reuse — see §2).

## 1. Purpose & scope

Let a user upload a file (PDF, DOCX, TXT, Markdown) and have its text content become searchable alongside their Gmail documents — the frontend already has an "Upload files" button, currently hidden (`Unified Search Interface.html`, `upload-btn hidden`, comment: *"File uploads aren't built yet (no backend endpoint)"*). This spec builds that endpoint.

### In scope
- `POST /sources/upload` — multipart file upload, one file per request.
- Text extraction for PDF, DOCX, TXT, `.md` — a new `TextExtractor` port + one adapter dispatching on MIME type.
- Raw file storage (the original file is kept, not discarded after extraction — see §3) — a new `FileStorage` port + a `LocalFileStorage` adapter (files under a Docker-volume-backed directory).
- A new `SourceType.FILE` and a lazily-created, one-per-user "Uploads" `SourceConnection` (see §2 for why this shape, not a new `SourceConnector`).
- `DELETE /documents/{document_id}` — removing one uploaded file (see §2.3; the existing `DELETE /sources/{connection_id}` deletes a whole *connection*, which is the wrong granularity here).
- Reusing the existing `DocumentRepository`/`SearchIndex` write paths — an uploaded file becomes a `Document` row and an Elasticsearch document exactly like a synced email does, just written by a new use case instead of `SyncSource`.

### Out of scope
- OCR / scanned-PDF text extraction — if a PDF has no extractable text layer, extraction yields an empty/near-empty body and that's surfaced as an error to the user (§5), not silently indexed as blank.
- Other file types (images, spreadsheets, `.eml`, `.zip` archives, ...) — additive later if wanted; the `TextExtractor` port is designed so adding one is a new branch, not a redesign (§2.2).
- Editing/replacing an uploaded file's content after upload — re-upload as a new file if the content changes. No update flow.
- File-size-based storage quotas per user, virus/malware scanning — real concerns for a multi-tenant production system, explicitly deferred (single-demo-user MVP; see §9).
- Making semantic search actually work — that's `specs/semantic-search.md`. This spec only needs uploaded documents to land in Postgres + Elasticsearch through the *existing* keyword-search path; semantic search then applies to uploaded documents, with no upload-specific work needed in this spec's code. After `semantic-search.md` §12, uploads are the only source that gets it.

## 2. Why not reuse `SourceConnector`?

Every OAuth source (Gmail; Slack/Notion when this was written, since removed — see `specs/remove-slack-notion.md`) fits the same shape: OAuth credentials, a `SourceConnector.fetch_changes(credentials, cursor)` pulled on a timer by the background scheduler. A file upload is the opposite of that shape — push-based (the user acts once, right now), no OAuth, nothing to poll. Forcing it through `SourceConnector` would mean a connector whose `fetch_changes` does nothing (polled uselessly every scheduler tick for a source that never changes on its own) — a leaky abstraction. So uploads get their own application use case (`UploadFile`, §4), not a `SourceConnector` implementation.

### 2.1 But it still needs a `SourceConnection` row

`documents.connection_id` is a `NOT NULL` foreign key to `source_connections` (`models.py`) — every document belongs to some connection, and that's worth keeping true rather than special-casing uploads with a nullable FK. So: **one `SourceConnection` per user, `source_type = FILE`, created lazily on that user's first upload** (`external_account = None`, `display_name = "Uploaded files"`), holding every file that user ever uploads — the same one-connection-to-many-documents shape Gmail already has, just never going through OAuth or the connect/callback flow.

This means `/sources` (the connections list) shows an "Uploaded files" row alongside Gmail once a user has uploaded anything, for free, with no frontend change — `_to_response()`'s `display_name` fallback already handles a connection with `external_account = None` correctly *as long as `display_name` is set* (it is, here), so no gap there.

### 2.2 Scheduler must skip `FILE` connections

`SourceConnectionRepository.list_active()` returns *all* active connections regardless of type — the background scheduler tick (`sync_scheduler.py`) loops over that list and calls `connector_factory.connector_for(connection.source_type, ...)` for each. `connector_for`/`oauth_provider_for` have no `FILE` branch and would raise `ValueError`. Fix: skip `FILE` connections in the tick's loop before calling the factory — there's nothing to sync, the upload endpoint already wrote everything there is to write. One line (`if connection.source_type == SourceType.FILE: continue`) at the top of the loop body, not a factory change.

Same reasoning applies to `POST /sources/{id}/sync` (manual resync) — resyncing an `Uploads` connection makes no sense (nothing external to re-fetch); return `409 Conflict` with a clear message, same status code already used for a disconnected connection.

### 2.3 Deleting one uploaded file, not the whole "Uploads" bucket

`DELETE /sources/{connection_id}` (existing) deletes a *connection* — for Gmail that's "disconnect the account," a natural single unit. For uploads, the natural unit a user wants to remove is *one file*, not "delete every file I've ever uploaded." So this spec adds `DELETE /documents/{document_id}`, scoped to the requesting user, which:
1. Looks up the `Document`, confirms `document.user_id == current_user.id` (404 otherwise — never leak whether another user's document id exists).
2. Deletes the row from Postgres (`DocumentRepository` gains a `delete_many`-shaped single-id path — see §6) and from Elasticsearch (`SearchIndex.delete_documents`).
3. Deletes the underlying file via `FileStorage.delete(storage_path)`.

This endpoint is deliberately generic (`/documents/{id}`, not `/sources/uploads/{id}`) — nothing about it is upload-specific except that it's the only source where per-document deletion will be reachable from the UI at first. A future "remove this one synced email from my index" feature (not currently planned) could reuse it unchanged.

## 3. Storage design

**Original file bytes are kept** (per the confirmed decision) — needed so `DELETE`'d aside, a user could later re-download or re-view what they uploaded, and so a future extraction bug/improvement could be fixed by re-running extraction against the stored original rather than needing a re-upload.

**Where**: a local disk directory, backed by a new named Docker volume (`findr_uploads_data`), mounted into the `app` service at a configurable path (`FINDR_UPLOAD_STORAGE_ROOT`, default `/data/uploads`) — the same pattern `docker-compose.yml` already uses for `findr_postgres_data`/`findr_es_data`. Chosen over Postgres `bytea` (would bloat the primary database with binary blobs it never needs to query) or S3-compatible object storage (a new external dependency/service this project doesn't otherwise have — `docker-compose.yml` has no MinIO today, and introducing one is a bigger step than a local-first MVP needs). **Explicitly revisitable**: if this app is ever deployed somewhere without a persistent local disk (e.g. a container platform that doesn't offer volumes), `FileStorage` is a port specifically so swapping `LocalFileStorage` for an S3 adapter later doesn't touch the application layer — noted as an open follow-up (§9), not designed further here.

**Layout**: `{FINDR_UPLOAD_STORAGE_ROOT}/{user_id}/{uuid4}{original_extension}` — the UUID (not the original filename) is the on-disk name, avoiding path traversal from a hostile filename and collisions between two uploads named `notes.pdf`. The original filename is preserved separately (§4, `uploaded_files.original_filename`) for display and for the `Document.subject` field.

**New table, not new `Document` fields**: file-specific metadata (original filename, MIME type, size, storage path) lives in a new `uploaded_files` table, one row per uploaded document, `document_id` as a foreign key to `documents.id` — *not* new nullable columns bolted onto `documents`/`Document`. This keeps `Document` (the domain entity every port/adapter already knows about — `DocumentRepository`, `SearchIndex`, the search router) completely unchanged; nothing about search, indexing, or the API response shape needs to know a document came from an upload versus Gmail. Same reasoning as why the Slack connector encoded `"{channel_id}:{ts}"` into `external_id` instead of `Document` growing Slack-specific fields (since removed — see `specs/remove-slack-notion.md`).

```python
class UploadedFileModel(Base):
    __tablename__ = "uploaded_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    original_filename: Mapped[str]
    mime_type: Mapped[str]
    file_size_bytes: Mapped[int]
    storage_path: Mapped[str]
    created_at: Mapped[datetime]
```

## 4. New ports & the upload use case

```python
class FileStorage(Protocol):
    def save(self, user_id: int, filename: str, content: bytes) -> str:
        """Persists the file, returns a storage_path/key to read or delete it by later."""
        ...
    def read(self, storage_path: str) -> bytes: ...
    def delete(self, storage_path: str) -> None: ...

class TextExtractor(Protocol):
    def extract(self, content: bytes, mime_type: str) -> str:
        """Raises UnsupportedFileType / ExtractionFailed (new domain exceptions) rather
        than returning an empty string, so a bad upload is a clear 4xx, not a silently
        empty, unsearchable document."""
        ...
```

`TextExtractor` is one adapter (`FileTextExtractor`), branching on `mime_type` internally — same "one branch per type in one place" shape as `connector_factory.py` — using `pypdf` (PDF), `python-docx` (DOCX), and a plain UTF-8 decode (TXT/Markdown). New dependencies: `pypdf`, `python-docx`.

**`UploadFile`** (new application use case, `application/uploads/upload_file.py`):
1. Extract text via `TextExtractor` (raises → `422 Unprocessable Entity` in the router, file never saved).
2. Save raw bytes via `FileStorage.save`.
3. Get-or-create the user's `FILE`-type `SourceConnection` (`SourceConnectionRepository.get_by_account(user_id, SourceType.FILE, external_account=None)`, `.create(...)` if missing).
4. Build a `Document`: `external_id` = a fresh UUID (uploads have no natural external id the way a Gmail message does), `subject` = original filename, `sender`/`recipients` = `None`, `body_text` = extracted text, `sent_at` = None, `thread_id` = None.
5. `DocumentRepository.upsert_many([document])` → real id back.
6. Insert the `uploaded_files` row (`document_id` = the id from step 5).
7. `SearchIndex.index_documents([document], SourceType.FILE, external_account=None)` — same call shape `SyncSource` already makes.

Steps 2–7 aren't wrapped in a single DB transaction with automatic rollback-and-delete-the-saved-file-on-failure — see §5's edge case table for what happens if a later step fails after the file is already saved.

## 5. Edge cases

| Edge case | Handling |
|---|---|
| Unsupported file type / wrong `Content-Type` | `TextExtractor` raises before anything is saved → `422` with a clear message. Nothing written to disk or Postgres. |
| PDF with no extractable text (scanned image, no text layer) | Extraction yields empty/whitespace-only text → treated as a extraction failure (`422`), not indexed as an empty document. OCR is out of scope (§1). |
| File saved to disk, then a later step fails (Postgres write, embedding, ES index) | The file is now an orphan on disk with no `documents`/`uploaded_files` row pointing at it. Accepted for this MVP (matches the existing "self-heal via retry" philosophy elsewhere in this codebase would need a *retry*, but there's nothing to retry here — the user just re-uploads) rather than building saga/cleanup machinery for a single-writer, single-demo-user system. A future cleanup script (list files on disk with no matching `uploaded_files` row, delete them) is a reasonable follow-up if orphans ever become a real problem — not built here. |
| Very large file | No size limit enforced in this spec — added as an open follow-up (§9) rather than picking an arbitrary number now. |
| Two uploads with the same filename (same user) | Fine — `external_id` is a fresh UUID per upload, not derived from filename, so no collision; both are kept as separate documents. |
| Deleting a document that doesn't belong to the requester | `404`, not `403` — same "don't confirm the id exists" reasoning already used elsewhere (e.g. `get(connection_id, user_id)` returning `None` rather than distinguishing "not found" from "not yours"). |

## 6. Port changes to existing interfaces

`DocumentRepository` gains a single-id delete, since `delete_many` is keyed by `(connection_id, external_ids)` — natural for "these documents just vanished from the source during a sync," awkward for "delete this one document I already have the id for":

```python
class DocumentRepository(Protocol):
    def upsert_many(self, documents: list[Document]) -> list[Document]: ...
    def delete_many(self, connection_id: int, external_ids: list[str]) -> None: ...
    def delete_by_id(self, document_id: int) -> None: ...  # new
```

No changes needed to `SearchIndex` for this spec specifically (`delete_documents(connection_id, external_ids)` already works for the one-document case — call it with a single-element list). Real `SearchIndex` changes come from the sibling `semantic-search.md` spec (embeddings), applying to every source including uploads, not designed here.

## 7. Testing strategy

Same split established in `postgres-migration.md`/`elasticsearch-search.md`: `TextExtractor` gets plain unit tests (no I/O — feed known byte content for each file type, assert extracted text); `LocalFileStorage` gets adapter tests against a temp directory (`tmp_path` fixture, no Docker dependency needed — it's just local disk); `UploadFile` (the use case) gets application-layer tests against fakes (`FakeFileStorage`, `FakeTextExtractor`, `FakeDocumentRepository`, `FakeSearchIndex`), mirroring `test_sync_source.py`'s pattern exactly. Router-level tests (`test_sources_router.py` or a new `test_upload_router.py`) exercise `POST /sources/upload` and `DELETE /documents/{id}` end-to-end against the real dockerized Postgres/Elasticsearch, via the existing `app_env` fixture.

## 8. Verification

**Automated**: unit tests for each extractor, adapter tests for `LocalFileStorage`, application tests for `UploadFile` against fakes, router tests for upload/delete against real Postgres/ES.

**Manual**: upload a PDF, DOCX, and `.txt` file through the UI, confirm each becomes searchable (once `semantic-search.md` also lands — or via existing keyword search before that, since this spec alone is sufficient for keyword search to work), confirm `/sources` shows an "Uploaded files" connection, delete one uploaded document and confirm it disappears from search results and from disk.

## 9. Open follow-ups (not decided here)

- File size limits / per-user storage quota — deferred; revisit if this becomes a real multi-tenant deployment rather than a demo/dev system.
- Virus/malware scanning on upload — same deferral reasoning.
- OCR for scanned PDFs — explicitly out of scope (§1), revisit as its own spec if wanted.
- Orphaned files on disk from a partially-failed upload (§5) — no cleanup mechanism built; revisit if it's ever observed to matter in practice.
- Swapping `LocalFileStorage` for an S3-compatible adapter for non-local deployment — the `FileStorage` port exists specifically so this is possible later without touching `UploadFile` or the router.

## 10. Implementation notes & deviations

Recorded at implementation time, per this project's practice of flagging where the code differs from the drafted text.

- **New `UploadedFileRepository` port** (`ports/uploaded_file_repository.py`, `create` / `get_by_document_id` / `delete_by_document_id`), not in the draft. The `uploaded_files` row needs a port like every other table, so the use cases never touch SQLAlchemy.
- **`DocumentRepository` gains `get(document_id, user_id)`** as well as the drafted `delete_by_id`. `DELETE /documents/{id}` needs a user-scoped lookup, returning `None` for both "missing" and "not yours", to get the `connection_id`/`external_id` for the index delete. `DeleteDocument` (`application/uploads/delete_document.py`) deletes in FK order: `uploaded_files` row, then `documents` row, then the index entry, then the file on disk last.
- **`SourceConnectionRepository.create`/`get_by_account` accept `external_account: str | None`.** `get_by_account(..., None)` matches with `IS NULL`. Postgres treats NULLs as distinct in the `(user_id, source_type, external_account)` unique constraint, so two concurrent *first* uploads could each create a FILE connection. That's not prevented, but `get_by_account` returns the oldest match rather than raising, so every later upload keeps working. Accepted for the single-demo-user MVP.
- **`DELETE /sources/{id}` on the FILE connection returns `409`,** mirroring §2.2's resync rule. The draft didn't cover this. Without it, `oauth_provider_for(FILE)` raises `ValueError`, which surfaces as a 500. The UI hides resync/disconnect for the "Uploaded files" row and puts a per-file **Delete** button on file search results instead.
- **MIME type resolution:** browsers often send `.md` (and sometimes `.docx`) as `application/octet-stream` or with no type. `resolve_mime_type(filename, declared)` in `file_text_extractor.py` uses a supported declared type if there is one, otherwise the extension. Files matching neither get `422`.
- **The upload runs in a threadpool** (`run_in_threadpool`): extraction and embedding (`semantic-search.md`) are CPU-bound and would otherwise block the event loop.
- **Search results for uploads have `url: null`.** There's no download endpoint yet; the originals are stored so one can be added later.
- **Frontend:** the "Upload files" button is now visible. It opens a multi-file picker and uploads one request per file, sequentially. Failures are reported together at the end.
- **Docker:** new named volume `findr_uploads_data` at `/data/uploads`. The runtime image creates it and `chown`s it to `findr` before `USER findr`, so the volume isn't root-owned.

## 11. Follow-on: chunking with Unstructured

`specs/upload-chunking.md` (draft) changes this spec in three ways:
- **Parsing:** the `TextExtractor` port and its `pypdf`/`python-docx` adapter (§4) are replaced by a `DocumentParser` backed by the `unstructured` library, adding OCR, table preservation and chunking.
- **Asynchronous uploads:** the §4 use case is split in two. The request validates, stores and enqueues, returning `202`; a background worker (Taskiq on Redis) parses and indexes.
- **Upload status:** `uploaded_files` gains a `status`, and parse failures become a `failed` status instead of a `422` (§5).

The storage design here (§3) is otherwise unchanged.
