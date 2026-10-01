# Spec: Migrate Search from SQLite FTS5 to Elasticsearch

Status: **Implemented** (`feature/postgres-elasticsearch-migration`) — see the "Deviation"/"Found during implementation"/"Correction" notes below for where the build diverged from this spec's original text.
Owner: findr
Related: `CLAUDE.md` ("Search" principle: keep indexing/retrieval behind a port so semantic search can be added later without touching domain/use-case code — this spec is that plan being executed), `specs/postgres-migration.md` (sibling spec — Postgres becomes the system of record in the same cutover; this spec covers the search-index side), `specs/gmail-thread-id.md` (a prerequisite — its `thread_id` field belongs in this spec's mapping, see §4).

## 1. Purpose & scope

Replace `SearchIndexSqlite` (SQLite FTS5) with an `ElasticsearchIndex` adapter implementing the existing `SearchIndex` port, so search runs against a real Elasticsearch cluster instead of a query joined directly against the primary database's tables. Postgres (per the sibling spec) remains the system of record for `documents`; Elasticsearch holds a denormalized, searchable copy kept in sync by the sync pipeline, not by database triggers (which don't exist across two separate data stores).

This also sets up the explicitly-planned future phase: semantic search via document embeddings. Elasticsearch 8.x has native `dense_vector` field support with kNN search — when that phase happens, it likely means adding one field to the mapping and a hybrid BM25+kNN query, not standing up a separate vector database. Nothing here builds that; it's why this direction was chosen.

### In scope
- Extending the `SearchIndex` port with write methods (`index_documents`, `delete_documents`), mirroring `DocumentRepository`'s shape.
- A new `ElasticsearchIndex` adapter: index provisioning, indexing, deletion, and the search query itself (replacing FTS5's `MATCH`/`bm25()`/`snippet()`).
- Wiring `SyncSource` to write to both Postgres and Elasticsearch after a successful fetch (the sync strategy — see §3 — including why this pattern is sufficient and how the index can be rebuilt from Postgres on demand).
- `scripts/reindex_search.py` — an on-demand backfill tool (Postgres → Elasticsearch), for bootstrapping a fresh environment or recovering from Elasticsearch data loss (§3.4).
- `docker-compose.yml` addition: single-node Elasticsearch for dev/CI (alongside the Postgres service from the sibling spec).
- Adapter-level tests against a real, dockerized Elasticsearch.
- Deleting `SearchIndexSqlite` and `schema.sql`'s FTS5 portion once verified.

### Out of scope
- **Embeddings / semantic search** — explicitly a future phase. The index mapping in §4 does not include a vector field yet; adding one later is an additive mapping change, not a redesign.
- Any change to `search_router.py`'s `SearchHitResponse` shape or the frontend — this migration must be invisible to API consumers. Same request/response contract, different implementation underneath.
- Filtering by source type, file type, or date at the query level — the frontend's Filters panel is already cosmetic-only (per prior decision); this spec doesn't change that.
- Relevance tuning beyond parity with the current BM25 ranking — see §5.

## 2. Port changes

```python
class SearchIndex(Protocol):
    def search(self, user_id: int, query: str) -> list[SearchHit]: ...
    def index_documents(
        self, documents: list[Document], source_type: SourceType, external_account: str | None
    ) -> None: ...
    def delete_documents(self, connection_id: int, external_ids: list[str]) -> None: ...
```

**Deviation from the design above, found during implementation**: `index_documents` also takes `source_type`/`external_account`, not just `documents`. `Document` doesn't carry either — they're connection-level facts, not document-level ones (see `SearchHit`'s own split in `domain/entities.py`) — and unlike Postgres, Elasticsearch has no `source_connections` table to `JOIN` against at query time, so they have to be denormalized onto each document at index time instead. `SyncSource` already has the `connection` in scope when it calls `index_documents`, so passing them through costs nothing. Otherwise `index_documents`/`delete_documents` mirror `DocumentRepository.upsert_many`/`delete_many`'s shape as designed: both ports do the same two things (persist a batch; remove some by connection + external id) against their own store. This is an additive, backward-compatible port change: nothing that only calls `search()` (the search router) needs to change.

**Also changing, and required for §3.1 to actually work**: `DocumentRepository.upsert_many` currently returns `None` (see `ports/document_repository.py`). It needs to return `list[Document]` instead — the same documents, but with `.id` now populated with whatever Postgres assigned them:

```python
class DocumentRepository(Protocol):
    def upsert_many(self, documents: list[Document]) -> list[Document]: ...  # was -> None
    def delete_many(self, connection_id: int, external_ids: list[str]) -> None: ...
```

This is necessary because Elasticsearch's document `_id` is `documents.id` (§4) — a value that doesn't exist until *after* the Postgres write. The `Document` objects a connector produces (`GmailConnector`; `SlackConnector` and `NotionConnector` at the time, since removed — see `specs/remove-slack-notion.md`) all set `id=0` as a placeholder (see e.g. `gmail_connector.py`: `id=0,  # ignored by the repository on insert`) — that's fine for the insert itself, but `index_documents` needs the *real* id, not the placeholder, or every document in a batch would collide on `_id="0"` and overwrite each other in Elasticsearch. `document_repository_postgres.py`'s upsert statement adds `.returning(DocumentModel.id)` and assigns the result back onto each `Document` before returning the list — one extra value per statement, no extra round trip.

## 3. Sync strategy: keeping Postgres and Elasticsearch consistent

**Pattern: dual-write from the single existing sync pipeline — not CDC, not an outbox.** `SyncSource.execute()` is the *only* code path that ever writes to `documents` today (`DocumentRepository.upsert_many`/`delete_many` are called nowhere else in the codebase) — no other use case creates, updates, or deletes a document. That single-writer fact is what makes the simpler pattern below sufficient, and it's worth naming the alternatives and why they're not needed yet:

- **Change Data Capture** (Postgres logical replication → Debezium/Kafka → Elasticsearch): the standard answer when multiple independent write paths touch the source table and you need a guarantee that every change eventually reaches the index without coupling each writer to it. Rejected here as infrastructure solving a problem that doesn't exist yet — there's exactly one writer. **Revisit this if a second write path to `documents` is ever added** (e.g. a future bulk-upload feature that writes directly instead of going through `SyncSource`).
- **Outbox pattern** (write an event row in the same Postgres transaction as the document, a separate relay process drains it into ES): gives a transactional guarantee that the "needs indexing" fact is never lost even if the process crashes between the two writes. Rejected for now — more moving parts (an outbox table + a relay worker) than the actual failure mode justifies, given the idempotency argument in §3.2.
- **Chosen: direct dual-write inside `SyncSource.execute()`**, relying on idempotent retries for safety rather than a transactional or queued guarantee. Simplest option that's actually correct for a single-writer system.

### 3.1 The write path

Changes to the existing `try` block in `application/sync/sync_source.py`, right after the existing Postgres writes:

```python
if batch.upserts:
    persisted = self._document_repo.upsert_many(batch.upserts)   # now returns list[Document], ids populated
    self._search_index.index_documents(                           # new — indexed with real ids, not the id=0 placeholders
        persisted, connection.source_type, connection.external_account
    )
if batch.deleted_external_ids:
    self._document_repo.delete_many(connection.id, batch.deleted_external_ids)
    self._search_index.delete_documents(connection.id, batch.deleted_external_ids)  # new
```

`SyncSource.__init__` gains a `search_index: SearchIndex` constructor parameter. Note `index_documents` is called with `persisted` (`upsert_many`'s return value), **not** the original `batch.upserts` — see §2 for why passing the original list would silently break (every document in the batch would collide on Elasticsearch `_id="0"`).

### 3.2 Why this is safe without a distributed transaction

Postgres and Elasticsearch are two separate systems — nothing spans a transaction across both, so a crash between the two calls *can* leave them briefly inconsistent (a document lands in Postgres but not yet in ES, or a deletion removes it from one but not the other). This is **tolerated, not eliminated**, because every operation involved is idempotent and the next scheduler tick repeats it:

- `document_repo.upsert_many` — `ON CONFLICT DO UPDATE`, safe to re-run.
- `search_index.index_documents` — same ES `_id` (the Postgres `documents.id`), re-indexing just overwrites, safe to re-run.
- `search_index.delete_documents` — delete-by-query on `(connection_id, external_id)`, safe to re-run against an already-deleted document (matches nothing, no-op).

A failure on either side marks the connection `ERROR` (§3.3), which is precisely what triggers "try again next tick" — so the inconsistency window is bounded by `FINDR_SYNC_INTERVAL_SECONDS`, not open-ended, as long as the connection keeps getting scheduled.

This all assumes `documents` are only ever written or deleted via `SyncSource` — true of every code path in this codebase today, and, per current plans, true going forward too (no manual/direct deletion of `documents` rows is planned). If that ever changes (a future feature deletes documents some other way), Elasticsearch would need to be told about it too — worth remembering *if* that day comes, but not a scenario this spec designs a solution for now.

### 3.3 Failure semantics (decided)

If `index_documents`/`delete_documents` raises, it propagates up through the same `except Exception as exc:` handler already in `SyncSource.execute()` — the whole sync is marked `ERROR` with `last_error` set to the Elasticsearch failure, exactly like any other sync failure today. No new exception type, no soft-fail path. A soft-fail (log and keep `ACTIVE`) was considered and rejected: it would leave documents silently unsearchable with no existing mechanism to notice or fix that, which is worse than a visible, self-healing `ERROR` status.

### 3.4 Bootstrapping: populating Elasticsearch from an existing Postgres

§3.1–§3.3 cover *incremental* consistency (each sync tick keeps the two stores aligned going forward). They don't cover **standing up Elasticsearch from a Postgres that already has data** — a new environment, or recovering from Elasticsearch data loss. Since Postgres is the system of record, this needs a small, explicit backfill tool:

**New script: `scripts/reindex_search.py`.** Reads every document from Postgres in batches (paginated by `id`), calling `search_index.index_documents()` per batch. Run manually (`uv run python scripts/reindex_search.py`) — an operator-triggered action, not a scheduled job. Reuses the same `ElasticsearchIndex` adapter and `ensure_index()` call as the running app (§6).

## 4. Index design

One Elasticsearch index, `findr_documents` — not one index per tenant. Per-user isolation is enforced the same way it is today (SQLite's `WHERE d.user_id = :user_id` on every query, now an ES `term` filter on every query), not via infrastructure-level separation. Simpler operationally, and consistent with the existing single-database-many-tenants model.

**Document `_id`**: the Postgres `documents.id` (as a string). This makes `index_documents` a natural upsert — ES's index API with an explicit `_id` overwrites any existing document — and keeps the two stores' identity concept aligned (no separate id-mapping table needed).

**Mapping**:

```json
{
  "mappings": {
    "properties": {
      "user_id":       { "type": "keyword" },
      "connection_id": { "type": "keyword" },
      "external_id":   { "type": "keyword" },
      "source_type":   { "type": "keyword" },
      "external_account": { "type": "keyword" },
      "subject":       { "type": "text" },
      "sender":        { "type": "text" },
      "recipients":    { "type": "text" },
      "body_text":     { "type": "text" },
      "sent_at":       { "type": "date" },
      "thread_id":     { "type": "keyword" }
    }
  }
}
```

`thread_id` is a **prerequisite from `specs/gmail-thread-id.md`** — Gmail's conversation-grouping key, `keyword` (not `text`) since it's an exact-match field, never full-text searched. Included here for the same reason as the Postgres column: free to add before the index exists, a mapping change afterward otherwise.

`external_account` isn't in the design above — added during implementation alongside the `index_documents` signature change in §2, for the same reason: it was needed by `search_router.py`'s Slack deep-link building, via `SearchHit.external_account` (Slack since removed — see `specs/remove-slack-notion.md`; the field stays, see that spec §3) but has no source to `JOIN` from in Elasticsearch, so it's denormalized onto each document like `source_type`.

`delete_documents(connection_id, external_ids)` uses ES's delete-by-query filtered on `connection_id` + `external_id` terms — matching `DocumentRepository.delete_many`'s exact inputs, so the caller never needs to know an ES-internal `_id` to delete something.

## 5. Search query

Replaces FTS5's `documents_fts MATCH :query` + `bm25()` + `snippet()`:

- **Matching**: `multi_match` across `subject`, `sender`, `body_text` (subject boosted, e.g. `subject^2`), filtered by `term: {user_id: ...}` — the same mandatory per-user filter SQLite enforced via its `JOIN ... WHERE user_id`.
- **Ranking**: Elasticsearch's default similarity is BM25 — the *same* ranking algorithm SQLite FTS5's `bm25()` used. This is a deliberate parity point: relevance ordering shouldn't regress just from this migration.
- **Snippets/highlighting**: ES's native `highlight` API replaces FTS5's `snippet()`. Configured with `pre_tags: ["["]` / `post_tags: ["]"]` — the same bracket characters the FTS5 `snippet()` call used — so the frontend's existing `renderSnippet()` JS (which turns `[`/`]` into `<strong>` tags) needs **zero changes**. This is called out explicitly because it's an easy thing to silently break by picking ES's default `<em>` tags instead.
- **Sanitization**: FTS5's `_sanitize_fts_query()` (quoting each token to prevent `MATCH` syntax injection) has an ES equivalent concern — `multi_match` with the default `best_fields` type doesn't parse user input as a query-string DSL the way FTS5's raw `MATCH` string did, so the SQL-injection-shaped risk doesn't carry over the same way, but user input should still not be trusted verbatim into anything using `query_string`/`simple_query_string` syntax. Use `multi_match` (structured query object, not a parsed string), not `query_string`, and this class of issue doesn't arise.

## 6. Client & index provisioning

- New dependency: the official `elasticsearch` Python client (v8.x, matching an ES 8.x server).
- Index creation is idempotent and happens at startup, the same way `init_db()` creates Postgres tables — a new `ensure_index()` (or similar) called from `app.py`'s `lifespan`, checking if `findr_documents` exists before creating it with the mapping in §4.
- `scripts/reindex_search.py` (§3.4) calls this same `ensure_index()` before backfilling, so a fresh environment can go from "empty Postgres, no ES index" to fully populated with one command after a normal sync has run.
- **`index_documents` uses the client's Bulk API** (`elasticsearch.helpers.bulk`), sending the whole batch as one request — not a loop issuing one `index()` call per document. For the concrete case this spec keeps coming back to (an initial Gmail sync producing ~100-200 documents in one `ChangeBatch`), that's the difference between one HTTP round trip and 100-200 of them. Same reasoning as the per-message rate-limit problem already hit and fixed in `GmailConnector` (`specs/gmail-connector.md`) — many individual calls in a tight loop is exactly the pattern that trips a provider's request-rate limits, and Elasticsearch's bulk endpoint exists specifically so this doesn't have to happen.

## 7. `docker-compose.yml` addition

```yaml
services:
  # postgres service defined in specs/postgres-migration.md
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.15.0
    environment:
      - discovery.type=single-node
      - xpack.security.enabled=false   # local dev only — never for a real deployment
      - "ES_JAVA_OPTS=-Xms512m -Xmx512m"
    ports:
      - "9200:9200"
    volumes:
      - findr_es_data:/usr/share/elasticsearch/data

volumes:
  findr_es_data:
```

`xpack.security.enabled=false` is a local-dev-only simplification (no TLS/auth handshake to configure just to run the app locally) — production deployment needs real authentication, out of scope here (see §9).

## 8. Testing strategy

Same split as the Postgres spec: application-layer tests (`SyncSource` et al.) already use a `FakeSearchIndex` test double for the port — adding `index_documents`/`delete_documents` to that fake is a small, mechanical change, no real ES needed.

Adapter-level tests for `ElasticsearchIndex` run against the real, dockerized Elasticsearch from §7 (decided, matching the Postgres spec's testing-strategy answer) — each test uses a uniquely-named index (created fresh, deleted in teardown) to avoid cross-test pollution, since ES has no equivalent of SQLite's free-per-test-engine isolation or a lightweight transaction-rollback pattern. **Found during implementation**: the fresh index must be created via the real `ensure_index()` (§6's mapping), not left for Elasticsearch to infer dynamically on first write — dynamic mapping types `external_id`/`connection_id` as `text`/`long` instead of `keyword`, which silently breaks the exact-match `term`/`terms` queries `delete_documents` relies on (a `term` query against an analyzed `text` field doesn't match the unanalyzed literal). The `es_index` test fixture calls `ensure_index()` itself so every test using it gets the production mapping for free.

## 9. Edge cases

| Edge case | Handling |
|---|---|
| Elasticsearch unreachable during a sync | `index_documents`/`delete_documents` raises → whole sync marked `ERROR` (§3) → retried next tick, self-healing once ES is back. |
| Elasticsearch unreachable at startup (`ensure_index()`) | Fails loudly at startup, same "fail fast" stance as an unreachable Postgres. |
| A document exists in Postgres but was never successfully indexed (e.g. crash between the two writes) | Self-heals on the next sync tick for that connection (§3.2). If it doesn't — e.g. the connection is now `DISCONNECTED` and won't sync again, or an operator wants to be certain everything's indexed — `scripts/reindex_search.py` (§3.4) backfills the whole index from Postgres on demand. |
| Re-indexing an unchanged document | Idempotent — same `_id`, overwrites with identical content. |
| User input containing Elasticsearch query-string special characters | Not a concern with `multi_match` (§5) — no query-string DSL parsing of raw user input. |

## 10. Open follow-ups (not decided here)

- **Semantic search / embeddings** — the explicitly planned next phase. Likely shape: an `EmbeddingProvider` port, a `dense_vector` field added to the mapping in §4, and a hybrid BM25+kNN query. Not designed further here.
- **Deleting documents outside `SyncSource`** (manual SQL, a future admin/bulk-delete feature) isn't a scenario this spec designs for — no manual deletion is planned, and no such feature exists yet (see §3.2's closing note). If one is ever added, it needs to also call `search_index.delete_documents()` (or trigger a reindex), same as `SyncSource` already does.
- Elasticsearch security (`xpack.security.enabled=true`, real auth) for any non-local deployment — deferred alongside the Postgres spec's "production hosting" follow-up.
- Whether `findr_documents` needs an index alias / versioning strategy for future mapping changes (e.g. adding the embeddings field without downtime) — revisit when that phase actually starts.

## 11. Verification

**Automated**: `FakeSearchIndex` gains `index_documents`/`delete_documents` for existing `SyncSource` tests to assert against (mirroring how `FakeDocumentRepository` already works in those tests). New adapter tests for `ElasticsearchIndex` against the real dockerized ES: index a document, search finds it scoped to the right `user_id`, delete removes it, highlighting produces the same `[`/`]` markers the frontend expects. A test for `scripts/reindex_search.py`: seed Postgres directly (bypassing the sync path), run the script, confirm the documents are now searchable via ES.

**Manual end-to-end**: `docker compose up -d elasticsearch`, run the app, connect a source, wait for/trigger a sync, confirm documents appear in the ES index (`curl localhost:9200/findr_documents/_search`), search from the UI and confirm results + highlighting render identically to before the migration. Separately: wipe the ES index entirely (`curl -X DELETE localhost:9200/findr_documents`), run `scripts/reindex_search.py`, confirm search works again without needing to reconnect or resync anything.

**Correction, found during implementation**: the line above originally said disconnecting a source should remove its documents from ES. It doesn't, and this isn't a regression — `DisconnectSource` (`application/sources/disconnect_source.py`) only revokes credentials and marks the connection `DISCONNECTED`; it never touches `documents`, and neither did the old `SearchIndexSqlite` query filter them out (its `JOIN source_connections` only read `source_type`/`external_account`, with no status filter). A disconnected source's documents stay searchable in both the old and new system alike — out of scope for this migration, which is meant to be behavior-invisible (§1).
