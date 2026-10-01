# Spec: Semantic search (embeddings + hybrid BM25/kNN)

Status: **Implemented** (branch `feature/file-upload-semantic-search`). Deviations from the draft are recorded in §11. **§12 (semantic search scoped to uploaded files) is a later revision, now implemented.** Where §1–§11 say embeddings apply to every source, §12 supersedes them.
Owner: findr
Related: `specs/elasticsearch-search.md` §10 (this is that spec's explicitly-planned "next phase," now being scoped: *"an `EmbeddingProvider` port, a `dense_vector` field added to the mapping, and a hybrid BM25+kNN query"*), `specs/file-upload.md` (sibling spec — after the §12 revision, uploaded files are the *only* documents that get semantic search; still with zero upload-specific code outside the search adapter), `CLAUDE.md` ("Search" principle: *"Don't let the keyword-search implementation foreclose adding semantic search later — keep indexing/retrieval behind a port"* — this spec is written specifically to honor that: no domain or application code changes, see §2).

## 1. Purpose & scope

Add semantic (meaning-based) search on top of the existing BM25 keyword search, so a query like "renewal pricing" can match a document that says "updated subscription cost" with no literal word overlap. ~~Applies uniformly to every document already in the index — Gmail (today) and uploaded files.~~ **Revised (§12):** semantic search applies to **uploaded files only** (PDF, DOCX, TXT, Markdown), which get hybrid keyword + semantic search. Gmail stays keyword-only (BM25), and so does every other source.

### In scope
- A new `EmbeddingProvider` port + a local (in-process, no external API) adapter using `sentence-transformers`.
- A `dense_vector` field added to the Elasticsearch mapping (`es_client.py`'s `INDEX_MAPPING`).
- `ElasticsearchIndex.index_documents` computes and stores an embedding for each document's text — internally, behind the existing `SearchIndex` port, so `SyncSource` and `UploadFile` need zero changes (§2).
- `ElasticsearchIndex.search` becomes hybrid: BM25 (existing `multi_match`) combined with kNN over the embedding field via Elasticsearch's Reciprocal Rank Fusion (RRF), per the confirmed decision (one query, both signals) — also entirely internal to the adapter; `SearchIndex.search(user_id, query) -> list[SearchHit]`'s signature is unchanged.
- Backfilling embeddings for documents already indexed before this feature existed — for free, by re-running the existing `scripts/reindex_search.py` (§4), not a new script.
- `ensure_index()` updated to add the new field to an *existing* index's mapping (additive `put_mapping`), not just at index-creation time — needed because the local dev `findr_documents` index (and anyone else's already-running Elasticsearch) predates this field.

### Out of scope
- A frontend toggle between "keyword" and "semantic" modes — there is only one search now, hybrid by default, matching the confirmed decision. No new UI.
- Chunking long documents into multiple embedded windows — a single embedding is computed per document from (a truncated prefix of) its text; very long documents lose relevance on content past the model's max sequence length. Flagged as a real limitation (§6), not solved here — a legitimate follow-up if it turns out to matter (§9).
- Re-ranking with a cross-encoder or any LLM-based reranking step — RRF-combined BM25+kNN is the full extent of the ranking sophistication here.
- Changing `SearchHitResponse`'s shape or the frontend's snippet-rendering — same "invisible to API consumers" constraint `elasticsearch-search.md` held itself to; a hybrid-search result still returns the same JSON shape, snippet markers included.
- GPU acceleration / batched embedding throughput tuning — deferred, though flagged in §3 as more likely to actually be needed than it would have been with a smaller model, given the model choice below; revisit if CPU inference latency turns out to be a real problem rather than pre-optimizing for it now.

## 2. Port changes (or deliberately, the lack of them)

The whole point of `SearchIndex` being its own port (`search_index.py`'s docstring: *"Kept separate from DocumentRepository so a vector/semantic-search adapter can be added later without touching domain or application code"*) is exercised here directly:

```python
class EmbeddingProvider(Protocol):
    def embed_document(self, text: str) -> list[float]: ...
    def embed_query(self, text: str) -> list[float]: ...
```

**Two methods, not one** — a deliberate departure from the originally-drafted single `embed(text)`, forced by the model choice in §3: Qwen3-Embedding is an *instruction-tuned, asymmetric* retrieval model. Per its documented `sentence-transformers` usage, queries are encoded with `model.encode(queries, prompt_name="query")` (a named prompt template bundled in the model's config, applied internally by the library) while documents are encoded plain: `model.encode(document_chunks)`, no `prompt_name`. A single symmetric `embed()` method (fine for a symmetric model like MiniLM) would silently produce worse retrieval quality here — the port has to expose the query/document distinction so the adapter can pass the right `prompt_name` for each, one it's not free to collapse back into one method without losing what the model is actually tuned for.

This is still the **only** new port. `SearchIndex.index_documents(documents, source_type, external_account)` and `SearchIndex.search(user_id, query) -> list[SearchHit]` — both signatures from `elasticsearch-search.md` — **do not change**. `ElasticsearchIndex`'s constructor gains an `embedding_provider: EmbeddingProvider` dependency (alongside its existing `client`/`index_name`), and both `index_documents` and `search` call it internally:

- `index_documents`: for each document, `embedding_provider.embed_document(doc.body_text or "")` before building the bulk action, storing the vector under a new `embedding` field in `_source`. **Revised (§12):** only for uploaded files. Everything else is indexed without a vector.
- `search`: `embedding_provider.embed_query(query)` once, then issues the hybrid query (§5) instead of the plain `multi_match`.

**Consequence**: `SyncSource` (Gmail sync) and `UploadFile` (`specs/file-upload.md`) call `search_index.index_documents(...)` exactly as they already do today — neither needs to know embeddings exist. This is what "keep indexing/retrieval behind a port" was for; this spec is the payoff, not a new architectural decision.

## 3. Embedding model

**Model: [`Qwen/Qwen3-Embedding-0.6B`](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)** — chosen directly by you over this spec's original suggestion (`sentence-transformers/all-MiniLM-L6-v2`). Still local/in-process, no external embeddings API, no per-token cost, no new outbound dependency at query time — that part of the original decision is unchanged. What changes is everything about the model itself, and it's worth being explicit about the consequences rather than treating this as a drop-in swap:

- **Much larger**: 0.6B parameters, a decoder-based (causal transformer) embedding model built on the Qwen3 base architecture — not the small BERT-style bi-encoder (22M params, ~80MB) MiniLM is. Expect a multi-hundred-MB-to-low-GB model download and meaningfully higher CPU inference latency per `embed()` call than MiniLM. This is a real tradeoff for better embedding quality, not a free upgrade — flagged here rather than silently absorbed, since it changes the "CPU is fine at this scale" assumption the original draft made (§1's "GPU acceleration... out of scope" note is kept, but now genuinely worth watching rather than an easy call).
- **Embedding dimension: confirm against the model card at implementation time, not assumed here.** Qwen3-Embedding uses Matryoshka Representation Learning (MRL), so its output dimension is configurable within a supported range rather than fixed the way MiniLM's 384 is — whatever value is chosen becomes the ES mapping's `dims` (§1) and must stay consistent for every document ever indexed (changing it later means a full reindex, not a config tweak).
- **Asymmetric, instruction-tuned encoding** (§2): queries get a task-instruction prefix, documents don't. The adapter is responsible for applying the documented instruction template internally — callers of `EmbeddingProvider` just pass plain text.
- **Long native context window** (Qwen3's context length, well beyond MiniLM's ~256-token effective limit) — the "long document gets silently truncated and loses relevance" edge case from the original draft (§6) is much less likely to bite in practice, though embedding e.g. a full 32k-token document on every sync tick is still worth capping for latency/memory reasons regardless of what the model can technically accept — an implementation-time tuning choice, not a design fork.
- **Library, confirmed**: `sentence-transformers`, using its built-in `prompt_name` parameter — not a hand-rolled prefix string. Per the model's documented usage: `model.encode(queries, prompt_name="query")` for queries, plain `model.encode(document_chunks)` (no `prompt_name`) for documents. This maps directly onto the two-method port from §2 — `embed_query` calls `encode(text, prompt_name="query")`, `embed_document` calls plain `encode(text)` — and means the adapter doesn't need to know or construct the actual instruction-prefix text itself; `sentence-transformers` resolves `prompt_name="query"` against a prompt template bundled in the model's own config. Still to confirm during implementation: the minimum `sentence-transformers` version this model family requires.

**Loaded once**, at app startup, held on `app.state.embedding_provider` (same pattern as `app.state.es_client`/`app.state.session_factory`) — loading the model per-request would be needlessly slow, more so here than with MiniLM given the larger model. `scripts/reindex_search.py` and the background scheduler construct their own instance at their own startup, same as they already do for `ElasticsearchIndex` itself.

**New dependency**: `sentence-transformers` (which pulls in `torch`). **Known risk, to resolve during implementation**: `torch`'s default PyPI wheel bundles CUDA libraries even for CPU-only use, which would bloat the app's Docker image (`Dockerfile`, `specs/dockerize-app.md`) — already true with MiniLM, and now compounded by the model weights themselves also being considerably larger. The fix for the `torch` half is installing the CPU-only build explicitly (PyTorch publishes one via its own index, `--extra-index-url https://download.pytorch.org/whl/cpu`) in the Dockerfile's builder stage — noted here as a required implementation step, not optional, but the exact `uv`/pip incantation needs to be verified empirically against this project's `uv`-based build (like the Python 3.14 wheel-availability risk already flagged and resolved without issue in `specs/dockerize-app.md` §2). The model-weights half has no equivalent fix — a 0.6B model's weights are a fixed cost of this choice, baked into either the image (if downloaded at build time) or a volume (if downloaded at first startup) — which of the two is a small implementation decision, not designed further here.

## 4. Backfilling existing documents

**Revised (§12):** the backfill now embeds uploaded files only. Re-running it also *drops* any vectors previously stored on Gmail documents, since each document's `_source` is rewritten in full.

Every document already in Elasticsearch (synced before this feature existed) has no `embedding` field. Because embedding computation lives inside `ElasticsearchIndex.index_documents` (§2), **the existing `scripts/reindex_search.py` backfills embeddings automatically, unchanged** — it already reads every document from Postgres and calls `index_documents()` per batch; once that method computes and stores embeddings internally, re-running the same script does the job. No new script needed. Operators re-run it once after deploying this feature (same "operator-triggered, not scheduled" posture `elasticsearch-search.md` §3.4 already established).

## 5. Search query design

**Elasticsearch's `retriever` API with RRF** (Reciprocal Rank Fusion), combining two retrievers in one `_search` request:
1. A `standard` retriever running the existing BM25 `multi_match` (subject boosted, `user_id` filter) — unchanged from `elasticsearch-search.md` §5.
2. A `knn` retriever over the new `embedding` field (`k`/`num_candidates` tuned for this project's scale — exact values are a tuning detail, not a design fork), filtered to the same `user_id`.

RRF combines the two ranked lists by rank position, not raw score — deliberately chosen over manually summing/weighting BM25 and cosine-similarity scores, which live on incompatible scales (BM25 is unbounded, cosine similarity is `[-1, 1]`) and would need ad-hoc normalization to combine sensibly. RRF sidesteps that entirely.

**Verify empirically during implementation** (flagged rather than asserted, following this project's established practice of confirming Elasticsearch behavior against the real dockerized cluster rather than assuming from documentation alone — see `elasticsearch-search.md`'s own dynamic-mapping discovery): that `highlight` still produces the `[`/`]` snippet markers the frontend depends on when used alongside the `retriever` DSL (rather than the plain `query` parameter `elasticsearch-search.md` originally used it with). If it doesn't compose cleanly, the fallback is a separate highlight-only query for just the top results, or computing snippets from the stored `body_text` client-side — a decision to make with real query results in hand, not from documentation.

## 6. Edge cases

| Edge case | Handling |
|---|---|
| Document with empty/null `body_text` (e.g. a subject-only email) | `embedding_provider.embed_document("")` — verify empirically that Qwen3-Embedding handles an empty string without erroring (not guaranteed the same way MiniLM's behavior was known); worst case, skip embedding and index with no vector when text is empty, falling back to BM25-only for that one document. |
| Document text much longer than practical to embed on every sync tick | Qwen3's native context window is long enough that outright truncation-losing-relevance (the concern with MiniLM) is unlikely — but capping input length for CPU latency/memory reasons is still worth doing regardless of the model's technical ceiling (§3). Chunking into multiple windows remains out of scope (§1). |
| Elasticsearch unreachable, or the embedding model fails to load at startup | Same "fail fast" stance as every other startup dependency in this codebase (`ensure_index()`, `create_db_engine()`) — no silent degrade to keyword-only search. Worth noting: a 0.6B model failing to load (out-of-memory, corrupted download) is a more plausible failure mode in practice than MiniLM's was. |
| A document is re-indexed after an edit (upsert) | Embedding is recomputed from the new text and overwrites the old vector — same "same `_id`, overwrite" idempotency `elasticsearch-search.md` §3.2 already relies on for the rest of the document. |
| `reindex_search.py` run against a large existing document set | Embedding computation is CPU-bound and adds real per-document latency to what was previously a pure network-bound backfill — meaningfully more so with a 0.6B model than the originally-drafted MiniLM, given the size difference in §3. Not optimized (e.g. batched embedding calls) here; worth watching in practice more than the original draft assumed. |

## 7. `docker-compose.yml` / `Dockerfile` impact

No new service — the embedding model runs in-process inside the existing `app` container, not a separate microservice. The `Dockerfile`'s dependency-install layer changes (§3's CPU-only `torch` wheel), and the image (or a volume, per §3's closing note) now also carries a 0.6B model's weights — a materially bigger image/first-startup cost than the MiniLM-based original draft would have had, worth confirming is acceptable rather than assuming. Elasticsearch itself needs no version bump — `dense_vector`/kNN support has existed since long before the 8.15.0 already pinned in `docker-compose.yml`.

## 8. Testing strategy

Same split as every other adapter in this codebase: `EmbeddingProvider`'s local adapter gets a plain unit test (embed a string via both `embed_document`/`embed_query`, assert the returned vectors have the expected dimension — see §3 on confirming that number — and aren't all-zero) — no Docker dependency, it's in-process, though slower to run than MiniLM's equivalent would have been given the model's size (§3). `ElasticsearchIndex`'s existing adapter tests (`test_document_search.py`) extend to cover the hybrid path: index two documents with clearly different meanings, search with a query that's semantically close to one but shares no keywords, confirm it's found (the test BM25-alone would fail) — this is the one test that actually proves semantic search works, not just that the code runs. Highlighting's continued correctness (§5) gets its own explicit assertion, since that's the one thing not guaranteed to survive the `retriever` API switch untested.

## 9. Open follow-ups (not decided here)

- Chunking long documents into multiple embedded windows, to stop losing relevance on content past the model's max sequence length — real limitation (§1, §6), deferred until it's observed to matter.
- Cross-encoder re-ranking of RRF's combined top-k for higher precision — a further-future refinement, not needed for this to be a real improvement over BM25-only.
- Batched/async embedding computation if `reindex_search.py` or live sync throughput ever becomes a bottleneck at a larger document count than this project currently has.
- Turning semantic search on for Gmail (or a future source) later. After §12 this means changing one constant in the search adapter, then reindexing. It was deliberately not made configurable (§12.3). The main cost is sync throughput on CPU; batched embedding (above) would likely be needed first.
- Swapping the local model for a hosted embeddings API (Voyage/OpenAI) later, if model quality or CPU cost ever becomes a real constraint — the `EmbeddingProvider` port exists specifically so that's an adapter swap, not a redesign.

## 10. Verification

**Automated**: unit test for the embedding adapter's output shape; adapter tests for `ElasticsearchIndex` proving a semantically-related, keyword-dissimilar query actually surfaces the right document (§8) and that highlighting still works.

**Manual** (revised for §12; see §12.6): upload a file, search with a paraphrased query that shares no words with it and confirm it's found; confirm Gmail documents have no `embedding` field and a paraphrased query does *not* surface an email; confirm exact-keyword queries still find both emails and uploads.

## 11. Implementation notes & deviations

- **RRF is fused client-side, not with the `retriever` API (forced deviation from §5).** Against the dockerized 8.15.0 cluster on its default **basic** licence, the `rrf` retriever returns `403 security_exception: current license is non-compliant for [Reciprocal Rank Fusion (RRF)]`. It is a paid-licence feature. Starting a trial licence would expire after 30 days. Elasticsearch's own `query` + `knn` combination sums scores, which §5 explicitly rejects. So `ElasticsearchIndex.search` sends both legs in one `_msearch`:
  - the unchanged BM25 `multi_match`, with `highlight`;
  - a top-level `knn` with the same `user_id` filter.

  It then fuses them in Python with `1 / (60 + rank)` (`_reciprocal_rank_fusion`). This keeps §5's intent: rank-based fusion of both signals in a single round trip. It stays entirely inside the adapter, and the `SearchIndex` port is unchanged. If the cluster ever gets a licence with RRF, switching to the `retriever` DSL touches only this method.
- **§5's highlight risk resolved as a side effect.** Highlighting stays on a plain `query`, so `[`/`]` markers work exactly as before for any hit BM25 found. A hit found **only** by kNN has no highlight and uses the existing `_build_snippet` fallback (the first 200 characters of `body_text`). This is covered by `test_semantic_query_finds_document_with_no_keyword_overlap`.
- **kNN similarity floor (new).** kNN always returns its `k` nearest neighbours, however unrelated, so without a floor every query would match every document. The kNN leg sets `similarity` (a cosine threshold), defaulting to **0.4** and overridable with `FINDR_SEMANTIC_MIN_SIMILARITY`. Tuned against the real model on a probe set:
  - paraphrased-but-relevant query/document pairs scored 0.43–0.58;
  - unrelated pairs, including vague one-word queries like "new"/"old", scored at most 0.37.

  One weak relevant pair ("candidate cv" → a résumé) scored 0.27 and is missed by kNN. That's the precision/recall trade-off this threshold makes, and it can be tuned per deployment.
- **Model facts confirmed (§3's open questions):**
  - output dimension **1024** (`EMBEDDING_DIMS` in `es_client.py`, `cosine` similarity);
  - the model config ships a `query` prompt (`Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:`);
  - requires **sentence-transformers ≥ 6** (for `get_embedding_dimension()`; the lock resolves 6.1 / transformers 5.17 / torch 2.14);
  - the adapter checks the dimension and the `query` prompt at load time and fails fast on a mismatch.
- **Input length cap:** `max_seq_length` is set to **512** tokens (`FINDR_EMBEDDING_MAX_SEQ_LENGTH`). At the model's default of 32k, a ~2,000-token document took ~6.6s to embed on CPU. Longer text is truncated; chunking is still §9 follow-up work.
- **What gets embedded:** `subject` + `body_text`, not `body_text` alone as §2 drafted. An email subject or an uploaded file's name is often the most meaningful text, and this gives subject-only documents a vector.
- **Empty text (§6):** the model embeds `""` without error, but a document with no subject and no body is indexed **without** an `embedding` field (BM25-only). A meaningless vector would only add noise to kNN. Documents without the field are skipped by kNN.
- **Vectors are excluded from `_source` in search responses** (`_source.excludes: ["embedding"]`), so each search doesn't ship 50×1024 floats back.
- **`ensure_index` on an existing index** calls `put_mapping` with the `embedding` field. This is additive and idempotent, as §1 drafted.
- **Device:** picked at load time by `detect_device()` — MPS (Apple silicon), then CUDA, then CPU. The Docker image ships CPU-only torch, so it always runs on CPU; a Mac dev machine uses MPS.
- **Wiring:** one `SentenceTransformerEmbeddingProvider` per process, on `app.state.embedding_provider`, shared by request handlers and the in-process scheduler (the model isn't loaded twice). `scripts/reindex_search.py` builds its own. Both go through a module-level `build_embedding_provider(settings)` so tests can swap in a fake.
- **Testing:** every test except the embedding adapter's own tests and the one real-semantic test uses a deterministic hashed bag-of-words `FakeEmbeddingProvider` (`tests/conftest.py`). Texts with disjoint vocabularies score ~0 cosine, below the floor, so existing keyword tests still mean "no shared words, no hit". The real model is loaded once per test session, and only by tests that need it.
- **Docker:**
  - CPU-only `torch` on Linux via a `[tool.uv.sources]` marker pointing at `https://download.pytorch.org/whl/cpu`; macOS dev machines resolve from PyPI.
  - Model weights download **at first startup** into a new named volume `findr_model_cache`, with `HF_HOME=/data/hf-cache`. They are not baked into the image. This keeps the image smaller and survives rebuilds; the cost is a one-off ~1.2GB download on first `docker compose up`.

## 12. Revision: semantic search for uploaded files only

Status: **Implemented**, together with `specs/upload-chunking.md`. Supersedes §1–§11 wherever they say embeddings apply to every source. **`specs/upload-chunking.md` (draft) builds on this section:** an upload is embedded as many chunks (nested vectors) instead of one vector. It changes *how* uploads are embedded; *which* sources are embedded stays as decided here.

### 12.1 Why

In practice, embedding every Gmail message on CPU in Docker makes Gmail sync slow: ~790% CPU for 31 messages, with no progress output. Semantic search matters most for uploaded documents, which are longer and written prose where wording varies. Email is well served by keyword search.

### 12.2 Decisions

| | Keyword (BM25) | Semantic (kNN) | Embedded at index time |
|---|---|---|---|
| Uploaded files (PDF, DOCX, TXT, Markdown) | yes | yes | yes |
| Gmail | yes | **no** | **no** |

- **Uploads keep hybrid search.** Keyword search still finds exact filenames and rare tokens such as invoice numbers, which semantic search can miss.
- **Still one combined result list,** fused with RRF as in §11. No UI change and no API response change.

### 12.3 Design

All changes stay inside the Elasticsearch search adapter. The `SearchIndex` and `EmbeddingProvider` ports, `SyncSource`, `UploadFile` and the Gmail connector are unchanged.

- **Which sources get embedded is fixed in code, not configurable.** A module-level constant in `search_index_elasticsearch.py`, `EMBEDDED_SOURCE_TYPES = frozenset({SourceType.FILE})`. There's no environment variable or constructor argument; controlling this isn't wanted for now. Changing it later is a one-line code change plus a reindex (§9).
- **`index_documents`:** if `source_type` is not in `EMBEDDED_SOURCE_TYPES`, the documents are indexed with no `embedding` field and `EmbeddingProvider` is **never called**. This is what makes Gmail sync fast again. For uploaded files, behaviour is unchanged from §11 (subject + body; no vector for empty text), until `specs/upload-chunking.md` replaces the single vector with per-chunk vectors.
- **`search`:**
  - **BM25 leg:** unchanged. It searches every document the user has, uploads included.
  - **kNN leg:** gains an explicit filter, `terms: {source_type: ["file"]}` (from `EMBEDDED_SOURCE_TYPES`), alongside the `user_id` filter. Only uploads carry vectors anyway, but the filter makes the rule explicit. It also guarantees that stale vectors left on Gmail documents from before this revision (§12.4) can never surface through semantic search.
  - **Query embedding:** still computed once per search. That's one short text, cheap compared with document embedding.
  - **RRF fusion and snippets:** unchanged from §11.
- **Wiring:** no changes to `Settings`, `.env.example` or any `ElasticsearchIndex` construction. The scheduler and reindex script still receive an `EmbeddingProvider`; for Gmail syncs it's simply never called.

### 12.4 Edge cases

| Edge case | Handling |
|---|---|
| Gmail documents already indexed **with** vectors (e.g. an environment where `reindex_search.py` was run before this revision) | The kNN `source_type` filter excludes them immediately. Their vectors are dropped the next time each document is re-indexed, by a sync update or by re-running `reindex_search.py`. No migration needed. The local dev index has none: the one Gmail sync that embedded messages was rolled back. |
| A user with no uploaded files | The kNN leg matches nothing and the results are pure BM25, the same as before semantic search existed. |
| Ranking between uploads and emails | An upload that matches on keywords *and* meaning scores in both legs and ranks above an email that matches keywords only. That's intended: a document that matches both ways is a stronger match. An upload that matches on meaning only competes by rank position with keyword-only email hits, as in §11. |
| Semantic search is later extended to Gmail (by editing `EMBEDDED_SOURCE_TYPES`) | Takes effect for newly indexed documents. Existing emails gain vectors only after re-running `reindex_search.py` (§4). Expect slow sync on CPU (§9). |

### 12.5 Testing

- **Adapter** (`test_document_search.py`):
  - indexing a `GMAIL` document stores **no** `embedding` field and doesn't call the embedding provider;
  - a `FILE` document does get a vector;
  - a paraphrased query finds an uploaded file but **not** an email with the same text;
  - a Gmail document written straight into Elasticsearch **with** a vector (simulating pre-revision data) is still excluded from semantic results;
  - keyword search still finds both.
- **Existing tests that exercise semantic behaviour with `SourceType.GMAIL`** switch to `SourceType.FILE`: the real-model test, the no-text test, the per-user isolation test, and the reindex test's embedding assertion.

### 12.6 Verification

- **Automated:** the tests above, plus the full suite green.
- **Manual (Docker):**
  - Gmail resync completes quickly, without the sustained high CPU;
  - `curl` a Gmail document from Elasticsearch and confirm there is no `embedding` field;
  - upload a file and confirm a paraphrased query finds it;
  - confirm a paraphrased query matching only an email's meaning returns nothing from Gmail, while a keyword query still does.
