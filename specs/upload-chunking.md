# Spec: Background processing of uploads — OCR, tables and chunking with Unstructured

Status: **Implemented** (branch `feature/file-upload-semantic-search`). What changed during implementation is recorded in §13.
Owner: findr
Related: `specs/semantic-search.md` §12 (semantic search for uploaded files only; this spec builds on it and changes *how* an upload is embedded), `specs/file-upload.md` (the upload flow this makes asynchronous and whose text extractor it replaces), `specs/dockerize-app.md` (the Python 3.13 move that made `unstructured` installable).

## 1. Purpose & scope

Three changes to how an uploaded file becomes searchable:

1. **Chunking.** Today an upload is one embedding: its filename plus the first 512 tokens (`semantic-search.md` §11), so everything past roughly the first page is invisible to semantic search. Each file is instead split into **chunks** that follow its structure (headings, paragraphs, tables), and every chunk is embedded.
2. **OCR and tables.** Scanned PDFs are read with OCR instead of being rejected, and **tables are preserved**: each table becomes its own chunk, never merged with surrounding prose, with its row/column structure stored as HTML.
3. **Background processing.** OCR and table detection take seconds to minutes per file on CPU and ~2GB+ of memory, far too slow for the upload request. The request stores the file and **enqueues a job**; a separate **asyncio worker** processes it from a **Redis** queue. The queue sits behind a port, so RabbitMQ is an adapter swap (§5.1).

Parsing, OCR, table detection and chunking all use the **`unstructured`** library (Unstructured-IO) in-process in the worker: its per-type **partition** functions and **`chunk_by_title`**.

### In scope
- Replacing the `pypdf`/`python-docx` extractor with `unstructured` partitioning for all four upload types: PDF, DOCX, TXT, Markdown.
- OCR for PDFs (scanned pages, and image regions inside text PDFs).
- Table detection and preservation for PDF, DOCX and Markdown.
- Chunking with `chunk_by_title`; per-chunk embeddings stored as nested vectors in Elasticsearch, with chunks in Postgres as the source of truth.
- **Metadata on every chunk,** including a **document version number** and a **content hash** of the original file. These lay the groundwork for handling a user uploading the same document again (§6.3), which a later spec will design.
- An asynchronous upload pipeline: a queue port, a Redis adapter (Taskiq), a worker service, upload status tracking, and status endpoints.
- A batch embedding method on the `EmbeddingProvider` port.
- Minimal frontend changes to show upload progress and failures.
- Docker changes: Redis service, worker service, and OCR system packages.

### Out of scope
- Chunking or background-processing Gmail. Gmail stays keyword-only (`semantic-search.md` §12) and keeps syncing via the existing scheduler.
- **New upload types**, e.g. images (PNG/JPG) or scanned TIFFs. The OCR stack would support them, but that's a follow-up (§12).
- **OCR of images embedded inside DOCX files.**
- **OCR languages other than English.**
- **Rendering tables as HTML tables in the search results UI.** The HTML is stored so a later UI change can use it; results still show plain-text snippets.
- **Configuration knobs** for chunk sizes, OCR, or which sources are chunked. These are constants in code. The only new setting is the Redis connection URL, which is infrastructure rather than a behaviour switch.
- Backfilling uploads made before this change. None exist in the dev environment (§9).
- **Deciding what "the same document" means, or what happens when one is re-uploaded** (replace, keep both, new version). This spec records the version and hash so that future design has what it needs; every upload is version 1 until then (§6.3).

## 2. What was verified before writing this spec

All checked empirically with `unstructured` 0.27.10 on Python 3.13, in a `python:3.13-slim-bookworm`-based container:

| Check | Result |
|---|---|
| Installs alongside current dependencies | Yes: torch 2.14, transformers 5.17 and sentence-transformers 6.1 unchanged. |
| System packages needed | `libxcb1 libgl1 libglib2.0-0` (without them `partition_pdf` fails to import: `libxcb.so.1` missing), plus `poppler-utils` and `tesseract-ocr` for `hi_res`/OCR. Tested with tesseract 5.3.0 and poppler 22.12.0. |
| **Scanned PDF** (image only), `hi_res` + `infer_table_structure` | **OCR works for prose:** heading and paragraph read correctly. **Table extraction was poor:** only the last row of a 4-row table survived. |
| **Text PDF with a table**, `hi_res` + `infer_table_structure` | Text complete; table detected as a `Table` element with all cells present, but the HTML column structure was **imperfect** (a header cell split across columns, some empty cells). |
| Text PDF, `fast` strategy | No table detected (cells came out as loose headings). So `fast` isn't enough if tables matter. |
| **DOCX with a table** | **Perfect:** every cell, clean HTML. Native to the format; no model involved. |
| **Markdown with a table** | **Perfect**, including `<thead>`. |
| `chunk_by_title` with tables | Each table becomes its own `Table` chunk with `text_as_html` preserved, never merged into prose. |
| Cost | Peak memory **~2.2GB** for partitioning. Text PDF `hi_res`: ~5s for one page. Scanned page: ~45s, including first-run model downloads. |
| Task queue on Python 3.13 | `taskiq` 0.13, `taskiq-redis` 1.2.4, `taskiq-aio-pika` 0.6.1 (RabbitMQ) all resolve. |

**Consequence:** "preserve tables" is fully met for DOCX and Markdown. For PDFs, table *text* is kept and tables are kept as separate chunks, but the *row/column structure* from the table model can be wrong, especially for scanned PDFs. That's a limit of `unstructured`'s default models, stated here as a known quality limitation rather than a bug to fix in this spec (§12). Numbers come from small synthetic documents and are re-checked on realistic files during implementation (§10).

## 3. Upload lifecycle

```
POST /sources/upload ─► validate type ─► save original ─► uploaded_files row (pending) ─► enqueue ─► 202
                                                                                                     │
worker ◄───────────────────────────────── Redis queue ◄─────────────────────────────────────────────┘
  │ claim (pending → processing)
  │ read original ─► partition (OCR, tables) ─► chunk ─► Document + chunks in Postgres
  │                                                    ─► embed chunks + index in Elasticsearch
  └─► ready (document_id set)   or   failed (error message)
```

**Statuses** on `uploaded_files.status`:

| Status | Meaning |
|---|---|
| `pending` | Stored and queued, not started. |
| `processing` | A worker has claimed it. |
| `ready` | Searchable. `document_id` is set. |
| `failed` | Couldn't be processed. `error` holds a user-facing message (e.g. "No text could be extracted"). |

**Synchronous vs. asynchronous checks:**
- **Still in the request (`422`):** unsupported type (`resolve_mime_type`, unchanged) and an empty (0-byte) file. These are cheap and certain, so the user finds out immediately.
- **Now in the worker (`failed` status):** anything that needs parsing, such as a corrupt file or a document with no extractable text even after OCR. Previously these returned `422` from the upload request (`file-upload.md` §5).

## 4. Port changes

### 4.1 `TextExtractor` → `DocumentParser`

```python
class ChunkKind(str, Enum):
    TEXT = "text"
    TABLE = "table"

@dataclass
class ChunkMetadata:
    """See §6.3 for where each field comes from."""
    chunk_index: int                 # 0-based position in the document
    document_version: int            # version of the uploaded document this chunk belongs to
    content_sha256: str              # hash of the original file's bytes
    filename: str
    mime_type: str
    page_start: int | None           # PDFs only: first and last page the chunk covers
    page_end: int | None
    section_title: str | None        # nearest heading the chunk falls under
    element_types: list[str]         # e.g. ["Title", "NarrativeText"], ["Table"]
    languages: list[str]             # detected languages, e.g. ["eng"]
    is_continuation: bool            # true if this chunk is a later piece of a split element
    parser_version: str              # e.g. "unstructured-0.27.10/chunking-v1"

@dataclass
class DocumentChunk:
    text: str                  # plain text: what gets embedded and shown as a snippet
    kind: ChunkKind
    table_html: str | None     # the table's structure, for TABLE chunks; None for TEXT
    metadata: ChunkMetadata

@dataclass
class ParsedDocument:
    text: str                      # full text: body_text, used for keyword search and highlights
    chunks: list[DocumentChunk]    # in document order; never empty

class DocumentParser(Protocol):
    def parse(
        self, content: bytes, mime_type: str, filename: str, document_version: int
    ) -> ParsedDocument:
        """Raises UnsupportedFileType / ExtractionFailed. Slow (OCR, layout
        models) and memory-hungry: call from the worker, never a request."""
        ...
```

### 4.2 `Document` gains `chunks`

```python
@dataclass
class Document:
    ...
    # Passages used for semantic search. Empty for documents that aren't
    # chunked (every source except uploads, per semantic-search.md §12).
    chunks: list[DocumentChunk] = field(default_factory=list)
```

This deliberately reverses `file-upload.md` §3's "keep `Document` unchanged" choice. Chunks describe a document's *content*, not upload-specific metadata, and any source could have them later. With chunks on `Document`, `SearchIndex.index_documents` and `DocumentRepository.upsert_many` keep their signatures, and `SyncSource` is untouched: Gmail documents simply have no chunks.

### 4.3 `EmbeddingProvider`: batch document embedding

```python
class EmbeddingProvider(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...  # replaces embed_document
    def embed_query(self, text: str) -> list[float]: ...
```

One file can produce dozens or hundreds of chunks. A single batch call is much faster than one call per chunk.

### 4.4 New: `UploadQueue`

```python
class UploadQueue(Protocol):
    def enqueue(self, upload_id: int) -> None:
        """Asks a worker to process this upload. Only the id travels —
        the worker reads everything else from Postgres and FileStorage,
        so a message stays tiny and a duplicate message is harmless."""
        ...
```

### 4.5 `UploadedFileRepository` gains status handling

```python
class UploadedFileRepository(Protocol):
    def create(self, user_id, original_filename, mime_type, file_size_bytes, storage_path,
               content_sha256, document_version) -> UploadedFile: ...
        # status=pending, document_id=None — no document exists yet
    def get(self, upload_id: int, user_id: int) -> UploadedFile | None: ...
    def list_for_user(self, user_id: int) -> list[UploadedFile]: ...
    def claim(self, upload_id: int) -> UploadedFile | None:
        """Atomically moves pending → processing (or re-claims a stale
        processing row, §7). Returns None if it's not claimable: already
        ready/failed, being processed by someone else, or deleted."""
        ...
    def mark_ready(self, upload_id: int, document_id: int) -> None: ...
    def mark_failed(self, upload_id: int, error: str) -> None: ...
    def list_stale(self, older_than: datetime) -> list[UploadedFile]: ...
    def get_by_document_id(self, document_id: int) -> UploadedFile | None: ...  # unchanged
    def delete(self, upload_id: int) -> None: ...
```

`UploadedFile` (the domain entity) gains `status`, `error`, `attempts`, `updated_at`, `content_sha256` and `document_version`, and its `document_id` becomes optional. `create` computes nothing itself: the use case passes the hash and version in.

### 4.6 Unchanged

`SearchIndex` (all methods), `DocumentRepository` (signatures), `FileStorage`, `SourceConnectionRepository`, `SyncSource`, and the Gmail scheduler.

## 5. Background processing

### 5.1 Queue and worker: Taskiq on Redis

- **Library: [Taskiq](https://taskiq-python.github.io/).** An asyncio-native task queue: tasks are `async def` and the worker runs an asyncio event loop. It has official brokers for both Redis (`taskiq-redis`) and RabbitMQ (`taskiq-aio-pika`), with the same task code.
- **Broker: Redis**, using `taskiq-redis`'s **stream broker** (`RedisStreamBroker`). Redis Streams keep a message until the worker **acknowledges** it, so a worker that crashes mid-job doesn't silently lose the message. Redis over RabbitMQ because it's one small container with no extra setup (exchanges, queues, users), which is enough for one job type at this scale.
- **Switching to RabbitMQ** means a new `UploadQueue` adapter and broker wiring using `taskiq-aio-pika`. The use cases, worker task and API don't change. Only one broker is wired at a time; there's no runtime switch between them.
- **Adapters:**
  - `TaskiqUploadQueue` (outbound, used by the API): `enqueue` sends the `process_upload` task with the upload id.
  - `process_upload` task (inbound, runs in the worker): a thin `async def` that calls the `ProcessUpload` use case.

### 5.2 CPU-bound work in an asyncio worker

Partitioning, OCR and embedding are **blocking, CPU-bound** calls. Run directly on the event loop, they would freeze the worker, including its connection to Redis. So the task hands the whole `ProcessUpload.execute(upload_id)` call to a thread with `asyncio.to_thread`. The heavy libraries (onnxruntime, torch, tesseract) do most of their work outside Python's GIL.

**One upload at a time per worker: one process, one job, fixed.** The jobs are CPU-bound. On CPU, one job already uses nearly all cores, so running more jobs at once mostly splits the same CPU between them, while memory grows with each (~3–4GB per worker process; §10). So neither the process count nor the concurrency is configurable for now; both are fixed at 1 in the worker's start command. If throughput ever needs to grow, run more worker containers (`docker compose up -d --scale worker=N`, memory permitting): each joins the same Redis consumer group, and the Postgres claim (§5.3) keeps them from processing the same upload twice. Making concurrency configurable is a follow-up for when the work stops being CPU-bound (a GPU, or hosted OCR/embedding APIs; §12).

**Consumer settings** (the `RedisStreamBroker` from `taskiq-redis`, checked against its source):

| Setting | Value | Why |
|---|---|---|
| `xread_count` | `1` | The number of messages fetched per read. The library default of 100 would let one busy worker hold up to 100 jobs while processing only one, starving the other workers; and if it crashed, all of them would wait out the redelivery timeout. |
| `--max-prefetch` | `1` | Same reason, at the Taskiq level: a process never holds more messages than it can run. |
| `idle_timeout` | 30 minutes | How long an unacknowledged message waits before Redis redelivers it (`XAUTOCLAIM`) to another worker. The library default is 10 minutes, shorter than a long OCR job, which would trigger pointless redeliveries. 30 minutes matches the "stale `processing`" bound (§7). |
| `--ack-type` | `when_executed` | Acknowledge only after the task has run, so a crash mid-job leaves the message to be redelivered. Set explicitly rather than relying on Taskiq's default. |
| `maxlen` | ~10,000 | Trims acknowledged messages from the stream (§10). |
| `consumer_group_name` | `taskiq` (default) | Every worker process joins this group; Redis gives each message to exactly one member. |

### 5.3 `ProcessUpload` use case (worker side)

1. **Claim** the upload (`claim`). If `None`, stop quietly: it's a duplicate or stale message, or the upload was already deleted.
2. **Read** the original bytes via `FileStorage.read`.
3. **Parse** via `DocumentParser.parse`, passing the upload's filename and `document_version`. The parser attaches metadata to every chunk (§6.3).
   - `ExtractionFailed` or `UnsupportedFileType`: `mark_failed` with the message, and stop. **No retry**, because parsing is deterministic.
4. **Get or create** the user's `FILE` connection. Unchanged from `file-upload.md` §4, just moved into the worker.
5. **Save** the `Document` (with chunks) via `DocumentRepository.upsert_many`, then index it with `SearchIndex.index_documents` (which embeds the chunks, §6).
6. **`mark_ready`** with the document id, and commit.

**Deleted while processing:** step 6 re-checks the upload row under a row lock. If the row is gone, the worker deletes the Document it just created, removes it from the index, and stops. No orphaned searchable document is left behind.

**Infrastructure errors** (Postgres, Elasticsearch or file storage unavailable, or an unexpected exception):
- the upload goes back to `pending` with `attempts + 1`, and the task is retried with backoff;
- after **3 attempts** it's marked `failed` with a generic message, and the error is logged.

### 5.4 Upload request (`UploadFile`, API side)

1. `resolve_mime_type`. If unsupported, raise `UnsupportedFileType` → `422`. If 0 bytes, raise `ExtractionFailed` → `422`.
2. Compute the SHA-256 of the bytes. `document_version = 1` (always, for now: §6.3).
3. `FileStorage.save`.
4. `UploadedFileRepository.create`, with status `pending`, the hash and the version.
5. Commit, **then** `UploadQueue.enqueue(upload.id)`. Enqueuing after the commit means the worker can never receive an id whose row isn't visible yet. If the enqueue itself fails, the row stays `pending` and the stale-upload sweep (§7) picks it up. The user still gets `202`.

## 6. Parsing, chunking and indexing

### 6.1 Partitioning (`UnstructuredDocumentParser`)

A new adapter, `adapters/outbound/files/unstructured_document_parser.py`, replaces `FileTextExtractor`. It calls one explicit partition function per type; the auto-detecting `partition()` is avoided because it needs `libmagic`.

| Type | Call | OCR | Tables |
|---|---|---|---|
| PDF | `partition_pdf(file=…, strategy="hi_res", infer_table_structure=True, languages=["eng"])` | Yes: scanned pages and image regions | Detected by the layout model; structure from the table model. **Imperfect (§2).** |
| DOCX | `partition_docx(file=…)` | No (§1) | Native, exact |
| Markdown | `partition_md(file=…)` | n/a | Native, exact |
| TXT | `partition_text(file=…)` | n/a | n/a |

`hi_res` is always used for PDFs, even ones with a text layer: the `fast` strategy doesn't detect tables at all (§2).

**Full text (`body_text`):** the non-empty text of all elements, joined with blank lines; tables contribute their cell text. If it's empty, raise `ExtractionFailed` ("No text could be extracted from this file, even with OCR"). Partition errors on corrupt files also become `ExtractionFailed`.

### 6.2 Chunking

`chunk_by_title` over the elements, with these constants in the adapter:

| Parameter | Value | Why |
|---|---|---|
| `max_characters` | 1500 | Hard ceiling. At roughly 4 characters per token, about 375 tokens, under the embedding model's 512-token cap, so no chunk is silently truncated. |
| `new_after_n_chars` | 1200 | Soft limit: start a new chunk at the next element boundary, so chunks usually end on a paragraph break. |
| `overlap` | 150 | When a long passage has to be split, the next chunk repeats its last ~150 characters, so a sentence cut at the boundary still appears whole in one chunk. |
| `combine_text_under_n_chars` | 200 | Merges tiny sections (a bare heading) into the next, instead of making a heading-only chunk. |

These are starting values, checked on realistic documents during implementation (§10).

**Tables:** `chunk_by_title` never merges a table with prose. Each table (or each piece of an oversized table, if it's split at `max_characters`) becomes one `TABLE` chunk:
- `text` is the table's cell text: what's embedded and shown as a snippet;
- `table_html` is its `text_as_html`.

Everything else is a `TEXT` chunk with `table_html = None`.

**What gets embedded:** each chunk's `text` on its own, without the filename (which keyword search already covers) and without the HTML (markup adds noise to the embedding).

### 6.3 Chunk metadata

Every chunk carries a `ChunkMetadata` (§4.1), built by the parser. Most fields come straight from `unstructured`: each chunk's `metadata` includes `filename`, `filetype`, `languages`, `page_number`, `is_continuation` and `orig_elements` (confirmed in the §2 probes).

| Field | Source |
|---|---|
| `chunk_index` | Position in the chunk list. |
| `document_version` | From the upload (§5.4). Always `1` for now. |
| `content_sha256` | SHA-256 of the original bytes, computed once at upload (§5.4) and copied onto every chunk. |
| `filename`, `mime_type` | From the upload (`unstructured` only knows a filename if it's given one, since we pass bytes). |
| `page_start`, `page_end` | Min/max `page_number` across the chunk's `orig_elements`. `None` for DOCX, Markdown and TXT, which have no pages. |
| `section_title` | The text of the nearest `Title` element at or before the chunk, tracked while walking the elements. `None` if the document has no headings before it. |
| `element_types` | Distinct `category` values of the chunk's `orig_elements`, in order. |
| `languages` | `unstructured`'s detected languages for the chunk. |
| `is_continuation` | `unstructured`'s flag for the second and later pieces of an element split at `max_characters`. |
| `parser_version` | `"unstructured-<installed version>/chunking-v1"`. The `unstructured` version is read at runtime (`importlib.metadata`). `chunking-v1` is a constant in the parser, bumped whenever the partition or chunking parameters change. |

**Why version and hash now, before duplicates are handled:**
- **`document_version`** gives every chunk an explicit version, so a future re-upload flow can add version 2 without migrating existing chunks. It could also make search consider only the latest version.
- **`content_sha256`** makes "is this the same file?" a cheap lookup instead of re-reading every stored original. It's also stored on `uploaded_files` (indexed per user), where a future duplicate check would look.
- **`parser_version`** lets a future reprocessing job find chunks made by older parsing settings.
- Keeping version and hash on every chunk, not only on the upload, means Elasticsearch results and chunk rows are self-describing. A search hit can say which version of which file it came from.

**Not in chunk metadata:** the embedding model. That's a property of the index, not the chunk: every vector in an index comes from the same model (`semantic-search.md` §3), and changing models means a full reindex.

### 6.4 Storage

**Postgres, `document_chunks`** (the source of truth):

```python
class DocumentChunkModel(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    chunk_index: Mapped[int]          # 0-based position in the document
    kind: Mapped[str]                 # "text" | "table"
    text: Mapped[str]
    table_html: Mapped[str | None]
    document_version: Mapped[int]     # a real column: future "latest version only" queries filter on it
    content_sha256: Mapped[str] = mapped_column(index=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB)  # the remaining ChunkMetadata fields
```

`document_version` and `content_sha256` are real columns because future duplicate and version handling will filter and join on them. The other metadata fields go in one `JSONB` column: they're read back whole, never queried individually, and new fields can be added without a migration.

Chunks live in Postgres so that `scripts/reindex_search.py` can still rebuild Elasticsearch from Postgres alone, without re-running OCR on every file. `DocumentRepositoryPostgres` owns this table:
- `upsert_many` replaces a document's chunk rows **only when the document has chunks**, so Gmail sync gains no extra queries;
- `get` loads chunks in order;
- the delete methods remove chunk rows before their documents (foreign key).

**Postgres, `uploaded_files` changes:** `document_id` becomes nullable (still unique and a foreign key). New columns: `status` (indexed), `error`, `attempts` (default 0), `updated_at`, `document_version` (default 1) and `content_sha256`, with an index on `(user_id, content_sha256)` for the future duplicate lookup.

**Elasticsearch:** a nested `chunks` field replaces the top-level `embedding` field from `semantic-search.md` §11:

```json
"chunks": {
  "type": "nested",
  "properties": {
    "text":       {"type": "text", "index": false},
    "kind":       {"type": "keyword"},
    "table_html": {"type": "text", "index": false},
    "embedding":  {"type": "dense_vector", "dims": 1024, "index": true, "similarity": "cosine"},
    "metadata": {
      "properties": {
        "chunk_index":      {"type": "integer"},
        "document_version": {"type": "integer"},
        "content_sha256":   {"type": "keyword"},
        "filename":         {"type": "keyword"},
        "mime_type":        {"type": "keyword"},
        "page_start":       {"type": "integer"},
        "page_end":         {"type": "integer"},
        "section_title":    {"type": "text", "index": false},
        "element_types":    {"type": "keyword"},
        "languages":        {"type": "keyword"},
        "is_continuation":  {"type": "boolean"},
        "parser_version":   {"type": "keyword"}
      }
    }
  }
}
```

Metadata fields are explicitly mapped (`keyword`/`integer`), not left to dynamic mapping, so future filters such as "latest `document_version` only" or "this `content_sha256`" are exact-match queries. `elasticsearch-search.md` already hit the dynamic-mapping pitfall once. `inner_hits` returns the matching chunk's metadata with its text, available for a later UI change (for example "page 4, section *Pricing*"). The API response shape doesn't change in this spec.

- **Still one Elasticsearch document per `Document`**, so ids, deletes, per-user filtering and RRF all keep working per document.
- **Chunk `text` isn't keyword-searchable:** keyword search stays on `body_text`, so words aren't counted twice.
- **Existing indices:** `ensure_index` adds `chunks` with an additive `put_mapping`. An old index keeps an unused `embedding` field, which is harmless.
- **Verified:** nested `dense_vector` kNN works on the basic licence (8.15.0), with top-level filters and `inner_hits`.

### 6.5 Search (`ElasticsearchIndex`)

- **Keyword (BM25) leg:** unchanged.
- **Semantic (kNN) leg:** targets `chunks.embedding`, with the same `user_id` and `source_type: file` filters and the same similarity floor. Each document scores by its **best-matching chunk**; `inner_hits` (size 1) returns that chunk's `text` and `kind`.
- **Fusion:** unchanged.
- **Snippets:**
  - a document found by keyword keeps its highlighted keyword snippet;
  - a document found **only** semantically shows the matching chunk's text (first 200 characters), not the file's opening lines;
  - if that chunk is a table, it's the table's cell text.
- **Response shape:** unchanged, so the frontend's search rendering doesn't change.
- **Similarity floor (0.4):** re-checked at chunk granularity against the real model during implementation. Adjusted only if the probe shows it's needed.

## 7. Stale uploads (lost or stuck jobs)

A job can get stuck because the enqueue failed after the commit, Redis lost the message, or the worker was killed mid-job. Postgres statuses are the source of truth, so a sweep can recover it:
- **Where it runs:** the existing APScheduler in the API process, every 5 minutes, via a new use case `RequeueStaleUploads`.
- **What it re-enqueues:**
  - uploads `pending` for more than 10 minutes;
  - uploads `processing` for more than 30 minutes (a generous bound for OCR of a long PDF on CPU).
- **Duplicates are harmless:** `claim` is atomic, so a job enqueued twice, or re-enqueued while a slow worker is still running, runs at most once at a time.
- The same 3-attempt limit applies.

## 8. API and frontend

### 8.1 Endpoints

| Endpoint | Change |
|---|---|
| `POST /sources/upload` | Now returns **`202 Accepted`** with `{upload_id, filename, mime_type, file_size_bytes, status: "pending"}`. `422` only for unsupported or empty files. |
| `GET /uploads` | **New.** The user's uploads, newest first: `{upload_id, filename, status, error, document_id, created_at, updated_at}`. |
| `GET /uploads/{upload_id}` | **New.** One upload, same shape. `404` if missing or not the user's. |
| `DELETE /uploads/{upload_id}` | **New.** Removes an upload in **any** status, including its Document, chunks, index entry and stored file if they exist. `404` if missing or not the user's. The way to clear a `failed` upload. |
| `DELETE /documents/{document_id}` | Unchanged. For a ready upload it also deletes the `uploaded_files` row. |

### 8.2 Frontend

- **After upload**, a small uploads list appears under the source row: filename plus status ("Processing…", "Failed: \<error\>").
  - It polls `GET /uploads` every few seconds while anything is `pending` or `processing`, then stops.
  - When an upload turns `ready`, its entry disappears and the current search re-runs.
  - A `failed` entry has a remove button (`DELETE /uploads/{id}`).
- **Search results:** unchanged.

## 9. Edge cases

| Edge case | Handling |
|---|---|
| Scanned PDF | OCR'd in the worker. Previously rejected with `422`. |
| PDF with no text even after OCR (blank pages, pure images) | `failed`: "No text could be extracted from this file, even with OCR". |
| Table in a PDF | Kept as its own chunk. Cell text is searchable; the structure (`table_html`) may be imperfect (§2). |
| Corrupt file | `failed` with a parse error message; no retry. |
| Very large PDF (hundreds of pages) | Minutes of worker time. The user sees "Processing…" and can keep searching. No size limit, still (`file-upload.md` §9). |
| Worker down | Uploads stay `pending`; they're processed when a worker comes back. Search works throughout. |
| Redis down during upload | The row is committed as `pending`; the enqueue fails; `202` is returned anyway; the sweep re-enqueues once Redis is back. |
| Same message delivered twice | The second `claim` returns `None`, so it's skipped. |
| Upload deleted while processing | The worker detects it at the final step, discards its results, and nothing is left searchable (§5.3). |
| Duplicate filenames | Unchanged: separate uploads, separate documents. |
| Uploads made before this change | They have no `document_chunks` rows, so after a reindex they're keyword-only until re-uploaded. None exist in dev. A backfill that re-parses stored originals is a follow-up. |
| Gmail documents | Unchanged: no chunks, no vectors, never touch the queue. |

## 10. Dependencies, Docker and resources

**Python dependencies:**
- **Add:** `unstructured[pdf,docx,md]`, `taskiq`, `taskiq-redis`.
- **Remove:** `pypdf` and `python-docx` as direct dependencies, since `unstructured` brings them. `python-docx` moves to the dev group for test fixtures.

**Docker:**
- **Runtime image:** `apt-get install --no-install-recommends libxcb1 libgl1 libglib2.0-0 poppler-utils tesseract-ocr`. The API and worker share one image.
- **New `redis` service:** `redis:7-alpine` (59MB image). Configured with:
  - `--appendonly yes`, so queued messages survive a restart;
  - `--maxmemory 64mb --maxmemory-policy noeviction`: a hard cap, but Redis refuses new writes rather than silently evicting queued jobs if it's ever reached;
  - a named volume `findr_redis_data` and a healthcheck.

  The API and worker depend on it.
- **Stream trimming:** Redis Streams keep acknowledged messages until they're trimmed. The broker is configured with a maximum stream length (approximate `MAXLEN`, e.g. 10,000 entries) so old messages don't pile up forever. The exact `taskiq-redis` option is confirmed during implementation.
- **New `worker` service:** the same image, command `taskiq worker <broker module>:broker --workers 1 --max-async-tasks 1 --max-prefetch 1 --ack-type when_executed`. Mounts `findr_uploads_data` (to read originals) and `findr_model_cache` (the embedding model plus `unstructured`'s layout and table models from Hugging Face, so they download once). Depends on postgres, elasticsearch and redis.
- **New setting:** `FINDR_REDIS_URL`, default `redis://localhost:6379/0`; set to `redis://redis:6379/0` in `docker-compose.yml`. Added to `.env.example`.

**Memory:**

| Service | Approximate memory |
|---|---|
| Redis | **~10MB empty; ~17MB with 10,000 queued messages** (measured: ~190 bytes per message) |
| Elasticsearch | ~1GB |
| API (embedding model, for query embedding) | ~0.7GB |
| Worker (layout/table/OCR models + embedding model) | ~3–4GB while processing (one process, one job). Each extra worker container from `--scale` adds about the same again. |
| **Total** | **~5–6GB** |

Docker Desktop on this machine has 7.7GB. That fits, but not by much. If the worker is killed for running out of memory, the job is retried (§5.3) and eventually marked `failed`; raising Docker Desktop's memory limit is the fix.

**Verify during implementation:**
- image size increase;
- worker memory on a multi-page scanned PDF;
- processing time per page on CPU;
- chunk sizes on realistic documents;
- the similarity floor at chunk granularity.

## 11. Testing

- **Parser** (`UnstructuredDocumentParser`):
  - DOCX, Markdown and TXT parse from bytes, with section-aligned chunks and no chunk over `max_characters`;
  - long passages overlap and tiny headings are merged;
  - a DOCX/Markdown table becomes a single `TABLE` chunk with exact `table_html`;
  - **metadata:** every chunk has the right `chunk_index` sequence, `document_version`, `content_sha256`, `filename` and `parser_version`; `section_title` follows the headings; PDF chunks have `page_start`/`page_end` and non-PDF chunks don't; split elements are flagged `is_continuation`.
  - **OCR and PDF-table tests** (a scanned PDF's prose is recovered; a text PDF's table is a `TABLE` chunk) **run only where `tesseract` and `poppler` are installed**, i.e. inside the Docker image, and are skipped elsewhere. On the Mac, `brew install tesseract poppler` enables them.
  - Error cases: blank PDF, corrupt file, unsupported type.
- **Use cases (fakes):**
  - `UploadFile` validates, hashes, saves, creates a `pending` row with `document_version = 1` and the hash, and enqueues **after** commit;
  - `ProcessUpload`: happy path → ready; parse failure → failed without retry; infrastructure error → pending with attempts incremented, then failed after 3; claim returning `None` → no-op; deleted mid-processing → document removed;
  - `RequeueStaleUploads` re-enqueues only stale rows.
- **Repositories (Postgres):** chunk round-trip and replace, including all metadata (columns plus JSONB); Gmail documents don't touch `document_chunks`; the `claim` race (two claims, one winner); status transitions; stale listing.
- **Elasticsearch adapter:**
  - nested chunks with vectors for uploads, none for Gmail;
  - one `embed_documents` call per document;
  - a semantic-only hit's snippet is the matching chunk;
  - table chunks are indexed and matchable;
  - chunk metadata is stored in the nested mapping with explicit types, and `inner_hits` returns it.
  - **The test that proves chunking works (real model):** a long upload whose relevant passage sits well past the first 512 tokens. A paraphrased query finds it; the single-embedding approach couldn't.
- **Queue:** Taskiq's in-memory broker in tests. The router test enqueues, the task runs inline, and `GET /uploads/{id}` shows `ready`.
- **Router (end to end):**
  - `202` and a `pending` status on upload;
  - `422` for unsupported/empty;
  - `GET /uploads` shows only the user's uploads;
  - `DELETE /uploads/{id}` works in every status and `404`s for other users;
  - `DELETE /documents/{id}` still works for ready uploads.
- **Manual (Docker):**
  - upload a scanned PDF, a PDF with a table, a DOCX with a table and a long Markdown file; watch them go from pending to processing to ready in the UI;
  - search a paraphrase of a late section and of a table's contents;
  - kill the worker mid-job and confirm the sweep recovers it;
  - check `docker stats` for worker memory.

## 12. Open follow-ups (not decided here)

- **Duplicate uploads and versioning:**
  - what counts as "the same document" (same hash, same filename, or the user's choice);
  - what re-uploading does (reject, replace, or add version N+1);
  - whether search shows only the latest version.

  §6.3 records `document_version` and `content_sha256` on uploads and every chunk so this can be designed without migrating data.

- Better PDF table structure: `unstructured`'s alternative table models or a hosted parser, if the default model's structure (§2) proves inadequate on real documents.
- Image uploads (PNG/JPG/TIFF) via `partition_image`, since the OCR stack will already be installed.
- OCR languages beyond English, and OCR of images inside DOCX.
- Showing tables as real tables in search results, using the stored `table_html`.
- Backfilling chunks for pre-change uploads by re-parsing their stored originals.
- Moving Gmail sync onto the same worker/queue, instead of the API process's scheduler.
- Separate, slimmer images for the API and the worker (the API doesn't need OCR packages).
- Configurable worker processes/concurrency, once the work is no longer CPU-bound (a GPU, or hosted OCR/embedding APIs).
- Language detection for `ChunkMetadata.languages` is unreliable on short or repetitive text: a test string was labelled `cat` (Catalan). OCR is English-only (§1), so this only affects the metadata field, not parsing.

## 13. Implementation notes & deviations

**Corrections to §2** (found running the real stack as the non-root container user):
- **`unstructured` does download a model at runtime:** the spaCy model `en_core_web_sm`, which it uses for text classification. §2 said no runtime downloads were needed. `unstructured` installs the model into `site-packages` on first use, which the container's `findr` user can't write, so DOCX and Markdown uploads failed. It didn't show up earlier because the probes ran with a writable environment and never hit that code path. **Fix:** `en-core-web-sm==3.8.0` is a declared dependency, sourced from the same wheel URL `unstructured` pins; the lock records the same SHA-256 (`1932429d…`). A test guards it.
- **`torchvision` must come from the CPU-only index too.** `unstructured-inference` pulls it in. A PyPI `torchvision` next to the `+cpu` `torch` fails at import: `operator torchvision::nms does not exist`. It's declared directly so `[tool.uv.sources]` can point it at `pytorch-cpu` on Linux (uv sources only apply to direct dependencies).
- **Container environment:**
  - `NUMBA_CACHE_DIR=/tmp/numba-cache`: `unstructured` uses Numba, which otherwise tries to write its cache into `site-packages`;
  - the `findr` user now gets a home directory (`useradd --create-home`) for fontconfig/matplotlib caches.

**A risk found and closed: tables can disappear silently.** If `unstructured`'s table-structure model can't load during a parse (a failed download, or a cache the worker can't read), `partition_pdf` doesn't raise. It drops the table's content from the document. This was reproduced when a test run as root left root-owned model files in the shared cache volume.
- **Fix:** the worker calls `UnstructuredDocumentParser.warm_up()` at startup. It loads the layout model, the table agent and the spaCy model, so any problem stops the worker from starting instead of silently losing tables.
- **Bonus:** the first upload no longer waits for model downloads. Worker startup takes ~10s once models are cached.

**Other deviations from the text above:**
- **New `UnitOfWork` port** (`ports/unit_of_work.py`, Postgres adapter `UnitOfWorkPostgres`). `ProcessUpload` has to commit part-way: its claim must be visible, and no transaction may stay open during a minutes-long OCR run. `UploadFile` commits before enqueuing, and `RequeueStaleUploads` before re-enqueuing. Existing use cases still leave commits to their routers.
- **`UploadedFileRepository`** has `lock` (row lock for the final step and for delete) and `mark_retry` (returns the new attempt count). `claim` takes the stale threshold as an argument, so the use case owns the timing rules.
- **Retry backoff is a fixed 30-second delay** before re-enqueuing, not an increasing one. Infrastructure errors tend to be all-or-nothing, and the 3-attempt cap plus the sweep bound the total.
- **The stale sweep counts a stale `processing` upload as a failed attempt.** Otherwise a file that kills the worker every time (e.g. out of memory) would be retried forever.
- **Enqueuing from sync code:** the `UploadQueue` port is synchronous. `TaskiqUploadQueue` hands `kiq()` to the API's event loop with `asyncio.run_coroutine_threadsafe`, which works from FastAPI's threadpool and APScheduler's thread. The upload endpoint therefore runs the use case in a thread; calling it on the event loop itself would deadlock.
- **Broker details:** queue `findr_uploads`, consumer group `findr_workers`, `xread_count=1`, `idle_timeout` 30 min, approximate `maxlen` 10,000. Setting `FINDR_REDIS_URL=memory://` gives Taskiq's in-memory broker, used only by tests.
- **Parse-error classification:** partition errors become `ExtractionFailed` (failed, no retry), **except `OSError`**, which is treated as an environment problem and retried.
- **Chunk HTML:** chunking normalises a Markdown table's header cells from `<th>` to `<td>`; rows and columns are intact.
- **No migration tool:** `uploaded_files` changed shape. The table existed only on this branch and was empty in both the dev and test databases, so it was dropped and recreated by `create_all` rather than altered.

**Measured on the running stack:**

| Measure | Value |
|---|---|
| Image size (API and worker share it) | 3.67GB, up from 2.07GB |
| Idle memory | API ~690MB; worker ~670MB; Redis ~12MB |
| Worker peak while processing | 1.6–1.7GB for small documents (under the 3–4GB estimate; large scanned PDFs weren't measured) |
| Processing time | Scanned 1-page PDF ~8s; 2-page text PDF with a table ~19s; DOCX and Markdown a few seconds |
| PDF tables | Detected as their own chunk with correct pages, but header structure imperfect (`<td>\| Seats</td>`), as §2 predicted |

**Similarity floor at chunk granularity:** re-checked with the real model on the test documents.
- Relevant paraphrase → chunk pairs scored **0.45–0.63**, so the 0.4 floor keeps them.
- Unrelated queries mostly scored ≤0.31, **but the one-word query "new" reached 0.45** against a table chunk. At 0.4, vague one-word queries can pull in a loosely related upload. Accepted: RRF ranks it alongside keyword results rather than above them.
- **Note for local setups:** a `.env` that sets `FINDR_SEMANTIC_MIN_SIMILARITY` higher (0.7 was found in the dev `.env`) effectively disables semantic search, because relevant chunks score well under 0.7.

**End-to-end check (Docker):** scanned PDF, text PDF with a table, DOCX with a table and a long Markdown file all went pending → processing → ready. With the 0.4 floor, paraphrased queries found:
- the OCR'd memo, for "building rental agreement ending";
- the DOCX table, for "how much does the business tier cost";
- a page-2 passage;
- the last section of the handbook.

Semantic-only hits showed the matching chunk as their snippet. All 13 parser tests, including OCR and PDF tables, pass inside the image.
