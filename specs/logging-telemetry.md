# Spec: Logging and telemetry (no external tools)

Status: **Implemented** (branch `feature/logging`). Decisions in §11; what changed during implementation is recorded in §14.
Owner: findr
Related: `specs/upload-chunking.md` (the worker and its retry/sweep flow), `specs/gmail-connector.md` (background sync), `specs/workspaces.md` (the user/workspace scoping every log line should carry), `specs/dockerize-app.md` (where logs end up).

## 1. Purpose & scope

Today logging is ad hoc: a dozen modules call `logging.getLogger(__name__)`, but only the worker configures logging at all (`logging.basicConfig(level=INFO)` in `tasks.py`). The API relies on uvicorn's defaults. There is no request id, no user/workspace context, no timings. Several failures leave no log line at all, e.g. `SyncSource` records a failed sync on the connection's status but never logs the exception (§7).

This spec adds **structured logging with request correlation and timings**, using only the Python standard library. "Telemetry" here means **numbers carried on log events** (durations, counts, outcomes). Logs are written to **log files** as JSON lines, so these questions can be answered with `jq` or `grep` on the files. There are no metrics backends, tracing systems, error trackers or third-party logging libraries.

The design keeps a later move to real tooling (OpenTelemetry, Prometheus, Sentry, …) cheap. That move happens in the logging configuration and a few adapters. Call sites don't change.

### In scope
- One logging setup shared by the API, the worker and scripts: level and format from settings.
- Log files: one JSON-lines file per process, rotated by size, kept on a volume (§4.6).
- Request id per HTTP request, returned as `X-Request-ID`, and carried into the background upload job it enqueues.
- Context on every log line: `request_id`, `user_id`, `workspace_id`, and job ids (`upload_id`, `connection_id`) where they apply.
- One access-log line per request, with duration, replacing uvicorn's access log.
- A catalogue of named events with timings and counts for the important flows (§6).
- Closing the logging gaps where failures are currently silent (§7).
- Redaction rules for secrets and personal content (§8).
- Log rotation for the log files.

### Out of scope
- Any external tool or service: OpenTelemetry, Prometheus, Grafana, Sentry, Loki, ELK/Datadog, structlog/loguru.
- A metrics endpoint (`/metrics`) or in-process counters. Durations and counts live on log events only.
- Distributed tracing (spans). The request id gives a single correlation id across API → worker, nothing more.
- Health/readiness endpoints. A good follow-up, but separate (§10).
- Logging in the frontend (`Unified Search Interface.html`).
- Storing logs in Postgres or Elasticsearch, shipping log files anywhere, or alerting.

## 2. Decisions

| Question | Decision |
|---|---|
| Library | Python standard library only (`logging`, `contextvars`, `json`, `time`). No third-party packages for logging or telemetry. |
| Where logs go | **Log files** under `FINDR_LOG_DIR`, one per process, always JSON lines, rotated by size with the stdlib `RotatingFileHandler` (§4.6). **Files only: nothing on the console**, so `docker compose logs` and the uvicorn terminal stay quiet once logging is set up (§4.7). |
| Format | Always JSON lines. There's no console, so no text format and no `FINDR_LOG_FORMAT` setting. |
| Level | `FINDR_LOG_LEVEL`, default `DEBUG` (Q3). Set `INFO` to quiet it down. Noisy third-party loggers are capped separately at `WARNING` via `FINDR_LOG_LEVEL_LIBS` (§4.3). |
| Telemetry | Fields on log events (`duration_ms`, counts, `outcome`). No separate metrics API. |
| Port or not | No `Logger`/`Telemetry` port. Use cases already use stdlib `logging` (it's not framework or I/O, so the domain stays clean). Swapping to real tooling later is done in the handler/formatter setup. A `Metrics` port is added only when a metrics tool arrives (§10). |
| Personal content | Never logged: email bodies/subjects, document text, file contents, search query text (Q1), and anything naming a client: workspace names, upload filenames, Gmail addresses (Q2). Logs carry ids only. |

## 3. Log record shape

Every line, in JSON mode, is one object:

```json
{
  "ts": "2026-10-01T09:14:03.512Z",
  "level": "INFO",
  "logger": "findr.application.uploads.process_upload",
  "event": "upload.processed",
  "msg": "Upload 42 processed: ready",
  "service": "worker",
  "request_id": "6f1c0e2a9b3d4e7f",
  "user_id": 1,
  "workspace_id": 3,
  "upload_id": 42,
  "outcome": "ready",
  "chunks": 17,
  "duration_ms": 48211
}
```

- `ts`, `level`, `logger`, `msg`, `service` are always present. `service` is `api`, `worker` or `script`, set at setup.
- `event` is a stable dotted name from the catalogue (§6). Lines without one are free-form (third-party libraries, ad-hoc debug).
- Context fields (`request_id`, `user_id`, `workspace_id`, `upload_id`, `connection_id`) come from context variables (§5), so call sites don't pass them.
- Event-specific fields are passed with `extra={...}`.
- Exceptions add `exc_type`, `exc_message` and `stack` (the formatted traceback as one string).

## 4. Components

A small package, `src/findr/observability/`. It's infrastructure, imported by adapters and entry points. Use cases only use `logging.getLogger(__name__)` and the `timed` helper.

### 4.1 `observability/logging_setup.py`
- `configure_logging(settings, service: str) -> None`. Idempotent. Installs one handler on the root logger: the rotating file handler (§4.6), with the JSON formatter, the context filter (§5) and the redaction filter (§8). Sets levels (§4.3) and takes over the console handlers other libraries install (§4.7).
- Called first thing in: the API's `lifespan` (before the embedding model loads, so its timing is logged), the worker's `WORKER_STARTUP` (replacing `basicConfig`), and `scripts/reindex_search.py`.
- Uvicorn: `uvicorn.access` is silenced (the access middleware replaces it, §4.4). `uvicorn` and `uvicorn.error` lose their own console handlers and propagate to the root, so they land in the file (§4.7).

### 4.2 Formatter
- `JsonFormatter(logging.Formatter)`: builds the dict in §3 from the record, including any non-standard attributes set via `extra`, and dumps it with `json.dumps(default=str)`.

### 4.3 Third-party log levels
Capped at `WARNING` regardless of `FINDR_LOG_LEVEL`: `elasticsearch`, `elastic_transport`, `urllib3`, `httpx`, `httpcore`, `apscheduler`, `taskiq`, `sentence_transformers`, `transformers`, `unstructured`, `pdfminer`, `PIL`, `multipart`. The list lives in one constant. `FINDR_LOG_LEVEL` does not lift it, so the default `DEBUG` level stays readable; `FINDR_LOG_LEVEL_LIBS` (default `WARNING`) sets their level when library detail is needed.

### 4.4 Access-log middleware (`adapters/inbound/http/middleware.py`)
A pure ASGI middleware (not `BaseHTTPMiddleware`, to keep streaming uploads untouched) that, per request:
1. Takes `X-Request-ID` from the request if it's a sane value (≤ 64 chars, `[A-Za-z0-9-]`), otherwise generates 16 hex chars.
2. Sets `request_id` in the context (§5) and adds `X-Request-ID` to the response.
3. On completion logs `http.request` with `method`, `route` (the route template, e.g. `/workspaces/{workspace_id}/search`, not the raw path, so ids and query strings never appear), `status`, `duration_ms`. `user_id`/`workspace_id` come along from context if the request was authenticated.
4. On an unhandled exception logs `http.request.failed` at ERROR with the traceback, then re-raises (FastAPI still returns its 500).

Static `GET /` is logged like any other request. Level: `INFO` for 2xx/3xx, `WARNING` for 4xx except 401/404 (`INFO`), `ERROR` for 5xx.

### 4.5 `timed` helper (`observability/timing.py`)
```python
with timed(logger, "search.executed", workspace_id=ws.id) as t:
    hits = ...
    t["hits"] = len(hits)
```
Logs the event on exit with `duration_ms` and whatever fields were added. On an exception it logs the same event with `outcome="error"` at WARNING and re-raises. Plain stdlib (`time.perf_counter`), usable from use cases.

### 4.6 Log files
- **Directory:** `FINDR_LOG_DIR`. Default `./data/logs` when running on the host (next to `./data/uploads`; `data/` is already git- and docker-ignored). In Docker it's `/data/logs` (§9). Created at startup if missing; if it can't be created or written, startup fails with a clear error rather than silently logging nowhere.
- **One file per process.** Python's `RotatingFileHandler` is not safe with several processes writing and rotating the same file, and `docker compose up --scale worker=2` runs several workers. So each process writes its own file:
  | Process | File |
  |---|---|
  | API | `api.log` |
  | Worker | `worker-<hostname>-<n>.log`: the container id in Docker, plus taskiq's process index (`--workers N` runs `worker-0` … `worker-<N-1>`), so every worker process has its own file (§14) |
  | Reindex script | `reindex.log` |
- **Rotation:** `logging.handlers.RotatingFileHandler`, `FINDR_LOG_MAX_BYTES` (default 10 MB) × `FINDR_LOG_BACKUP_COUNT` (default 5) → at most ~60 MB per process (`api.log`, `api.log.1` … `api.log.5`). UTF-8, `delay=True` so the file opens on the first record.
- **Format:** always JSON lines (§3), so the files are always machine-readable. One record per line; tracebacks are inside the `stack` field, never spanning lines.
- **What goes in:** everything the root logger handles at the configured levels, including uvicorn's error/startup lines and third-party warnings. Not uvicorn's access log (replaced by `http.request`, §4.4).
- **Before setup:** anything logged before `configure_logging` runs reaches the console only, because there's no file yet (§4.7).
- **Reading them:** `tail -f data/logs/api.log | jq`, or `jq 'select(.request_id=="…")' data/logs/*.log` to follow one request across the API and worker files.

### 4.7 No console output
Once `configure_logging` has run, nothing is written to stdout/stderr by logging:
- The root logger gets the file handler, and any stdout/stderr `StreamHandler` already on it (e.g. from an earlier `basicConfig`) is removed. Other handlers are left alone, so pytest's `caplog` still works.
- Console handlers that libraries attach to their own loggers (`uvicorn`, `taskiq`, `huggingface_hub`, `transformers`, …) are removed and those loggers set to propagate, so their records go to the file instead. `reclaim_console_handlers()` does this for every logger, and runs again after the model libraries are imported (§14).
- Python warnings are routed into logging with `logging.captureWarnings(True)`, so they land in the file too.
- Python's `logging.lastResort` (which prints to stderr when no handler exists) never triggers, since the root always has the file handler.

What can still reach the console, by design:
- **Before setup:** uvicorn's first boot lines (`Started server process`, `Waiting for application startup`) and taskiq's worker boot lines, printed before our code runs.
- **Setup failure:** if the log directory can't be written, `configure_logging` raises and the process exits with that error on stderr. It has nowhere else to go, and a silent failure would be worse.
- **Crashes outside logging:** a hard crash (e.g. the worker killed for memory, a segfault in a native library) prints whatever the runtime prints. Docker shows these with `docker compose logs`, which is the place to look when a container won't start.
- **`print`:** none left in our code once the reindex script is converted (§7.4).

## 5. Context propagation

`observability/context.py` holds `contextvars.ContextVar`s for `request_id`, `user_id`, `workspace_id`, `upload_id`, `connection_id`, plus a `bind(**fields)` context manager that sets and restores them. A logging filter copies the current values onto every record.

| Where | How context gets set |
|---|---|
| HTTP request | Middleware sets `request_id`. `deps.get_current_user` binds `user_id`; `deps.get_workspace` binds `workspace_id`. FastAPI runs sync endpoints via `run_in_threadpool`, which copies context, so it reaches use-case code. |
| Upload job (API → worker) | `TaskiqUploadQueue.enqueue` sends the current `request_id` as a Taskiq **label** (`task.kicker().with_labels(request_id=…)`). The worker task binds `request_id` from `context.message.labels` plus `upload_id`; `ProcessUpload` binds `user_id`/`workspace_id` once it has claimed the upload. `asyncio.to_thread` copies context into the processing thread. |
| Retries | The task re-enqueues itself with the same `request_id` label, so every attempt of one upload shares it. |
| Scheduler (Gmail sync, stale sweep) | No request. Each tick generates its own id (`sync-<8 hex>` / `sweep-<8 hex>`) as `request_id`, and binds `connection_id`, `user_id`, `workspace_id` per connection. Re-enqueued stale uploads carry the sweep's id. |
| Scripts | `reindex-<8 hex>`. |

## 6. Event catalogue

Fields listed are in addition to the context fields. Durations are `duration_ms` (integer).

### API & lifecycle
| Event | Level | Fields |
|---|---|---|
| `app.started` | INFO | `service`, `env`, timings: `db_init_ms`, `es_ensure_index_ms`, `embedding_model_load_ms`, `embedding_model`, `embedding_device` |
| `app.stopped` | INFO | — |
| `http.request` | see §4.4 | `method`, `route`, `status`, `duration_ms` |
| `http.request.failed` | ERROR | `method`, `route`, `duration_ms`, exception |

### Auth & workspaces
| Event | Level | Fields |
|---|---|---|
| `auth.login.succeeded` | INFO | `user_id` |
| `auth.login.failed` | WARNING | `reason` (`bad_credentials`) — no username |
| `auth.logout` | INFO | — |
| `workspace.created` / `.renamed` / `.deleted` | INFO | `workspace_id` (never the name: it's the client's name); deletion adds `connections_revoked`, `uploads_deleted`, `documents_deleted`, `duration_ms` |

### Search
| Event | Level | Fields |
|---|---|---|
| `search.executed` | INFO | `duration_ms`, `embed_ms`, `es_ms` (Elasticsearch's `took`), `hits`, `query_chars` |

### Gmail
| Event | Level | Fields |
|---|---|---|
| `source.connected` / `source.disconnected` | INFO | `source_type`, `connection_id` (no email address) |
| `source.connect.failed` | WARNING | `source_type`, `reason` |
| `sync.tick` | INFO | `connections`, `succeeded`, `failed`, `duration_ms` |
| `sync.connection` | INFO / WARNING on failure | `source_type`, `outcome` (`ok`, `needs_reauth`, `error`), `upserts`, `deletes`, `full_resync` (cursor expired), `token_refreshed`, `fetch_ms`, `index_ms`, `duration_ms`; exception on failure |
| `sync.manual` | INFO | same fields, for `POST /sources/{id}/sync` |

### Uploads
| Event | Level | Fields |
|---|---|---|
| `upload.received` | INFO | `upload_id`, `mime_type`, `size_bytes` (no filename, Q2) |
| `upload.enqueue.failed` | WARNING | `upload_id`, exception (existing log line, renamed) |
| `upload.processing.started` | INFO | `attempt`, `queued_ms` (time since upload) |
| `upload.processed` | INFO / WARNING if `failed` | `outcome` (`ready`, `failed`, `retry`, `skipped`), `attempt`, `parse_ms`, `chunks`, `table_chunks`, `pages`, `ocr_used`, `embed_ms`, `index_ms`, `duration_ms`; `error_kind` on failure |
| `upload.deleted` / `document.deleted` | INFO | ids |
| `upload.sweep` | INFO when it requeued anything, else DEBUG | `requeued`, `upload_ids` |
| `worker.ready` | INFO | `embedding_model_load_ms`, `parser_load_ms` |

### Scripts
| Event | Level | Fields |
|---|---|---|
| `reindex.completed` | INFO | `documents`, `chunks`, `duration_ms` (replaces the `print`) |

Measurements that sit inside adapters (`es_ms`, `embed_ms`, `parse_ms`, `pages`, `ocr_used`) are logged by the adapter as DEBUG detail where the use case can't see them. Where the use case needs a number for its own event (e.g. `chunks`), it reads it from the value the port already returns. **No port signatures change** for telemetry; anything a port doesn't already return stays as an adapter-level DEBUG line.

## 7. Gaps to close

Found while surveying the current code:

1. **`SyncSource.execute` swallows exceptions silently.** It stores `last_error` on the connection but logs nothing, so a failing Gmail sync is invisible in logs. Log `sync.connection` with the exception (WARNING; `needs_reauth` without a traceback).
2. **The API never configures logging.** Our INFO lines (e.g. the embedding model load in `sentence_transformer_provider.py`) depend on uvicorn's setup and have no consistent format. Fixed by §4.1.
3. **No request correlation between API and worker.** An upload's API request and its worker processing can't be tied together. Fixed by §5.
4. **`scripts/reindex_search.py` uses `print`.** Becomes `reindex.completed`.
5. **`findr/__init__.py` had a leftover `print("Hello from findr!")`** behind a `findr` console script. Both removed (Q4), already done.

## 8. Redaction and privacy

Logs are less protected than the database, so they must never hold secrets or personal content.

- **Never logged:** passwords, session tokens/cookies, OAuth access/refresh tokens and codes, the `state` parameter, `Authorization`/`Cookie` headers, `FINDR_TOKEN_ENCRYPTION_KEY`/`FINDR_SESSION_SECRET`, email bodies/subjects/senders, document text, chunk text, file contents.
- **Never logged either:** search query text (only `query_chars`, Q1) and anything that names a client: workspace names, original filenames, Gmail addresses (Q2). Not even at DEBUG.
- **Enforced in two ways:**
  1. By construction: the event catalogue only passes ids, counts and enums.
  2. A redaction filter as a safety net. Any `extra` key matching `password|token|secret|authorization|cookie|code|state` (case-insensitive) is replaced with `"[redacted]"`.
- **Exception messages** can carry content (e.g. a Google API error echoing a request). Tracebacks are kept because they're needed for debugging, but adapter code that raises domain exceptions must not put tokens in messages. A test checks the OAuth adapter's error path. The same goes for client names: exceptions that end up in logs (e.g. a parse failure) must not include the filename or Gmail address. The existing log lines in `gmail_connector.py` and `delete_workspace.py` (which logs `storage_path`) are checked against this during step 5.
- The access log uses the route template, so query strings (`?q=…`) never appear.

## 9. Docker

- **Log files:** `app` and `worker` set `FINDR_LOG_DIR=/data/logs`, on a new named volume `findr_logs_data` in `docker-compose.yml` (production-style). The `Dockerfile` creates `/data/logs` owned by the `findr` user, like `/data/uploads`, so the volume is writable.
- **Dev:** `docker-compose.override.yml` bind-mounts `./data/logs:/data/logs` for both services instead, so the files are readable on the host at `data/logs/` without `docker exec`.
- **Console output:** nearly empty now (§4.7), so no Docker log-driver changes. `docker compose logs` remains the place to look only when a container fails to start.
- `FINDR_LOG_DIR`, `FINDR_LOG_LEVEL`, `FINDR_LOG_LEVEL_LIBS`, `FINDR_LOG_MAX_BYTES` and `FINDR_LOG_BACKUP_COUNT` added to `.env.example`.
- `README.md` gets a short "Logs" section: where the files are, and a couple of `jq` recipes, e.g. slow searches: `jq 'select(.event=="search.executed" and .duration_ms>1000)' data/logs/api.log`; everything for one request across the API and workers: `jq 'select(.request_id=="…")' data/logs/*.log`.

## 10. Follow-ups (not in this spec)

- `GET /health` (liveness) and `/ready` (Postgres, Elasticsearch, Redis checks), and a worker heartbeat.
- When an external tool is adopted: OpenTelemetry for traces/metrics (the request id maps to a trace id; `timed` blocks map to spans), a `Metrics` port, Sentry or similar for errors.
- Surfacing sync and upload failures in the UI beyond the current status fields.
- Sampling or rate-limiting if log volume ever becomes a problem.

## 11. Decisions

| # | Question | Decision |
|---|---|---|
| Q1 | Search query text | Never logged, at any level. Only its length (`query_chars`). |
| Q2 | Client names in logs | Never logged: no workspace names, upload filenames or Gmail addresses. Ids only. |
| Q3 | Default level | `FINDR_LOG_LEVEL` defaults to `DEBUG`. Third-party loggers have their own `FINDR_LOG_LEVEL_LIBS`, default `WARNING`. |
| Q4 | `findr` console script | Removed, along with its placeholder `main()`. Done ahead of the rest. |
| — | "No tools" | Python standard library only. |
| — | Where logs go | Log files only (one per process, JSON lines, rotated). Nothing on the console. |

## 12. Testing

Using pytest's `caplog` and the existing fixtures; no new test dependencies. `tests/conftest.py` points `FINDR_LOG_DIR` at a per-session temp directory, so test runs never write into `data/logs/`.

- **Log files:** `configure_logging` with `tmp_path` as `FINDR_LOG_DIR` writes `<service>.log` as valid JSON lines (one per record, tracebacks inside `stack`); the worker file name includes the hostname; rotation happens at `FINDR_LOG_MAX_BYTES` and keeps `FINDR_LOG_BACKUP_COUNT` backups; an unwritable directory fails setup with a clear error; calling setup twice doesn't add duplicate handlers.
- **Formatter:** JSON output parses, has the §3 fields, includes `extra` fields and exception fields.
- **No console output:** after `configure_logging`, records from our loggers, `uvicorn.error`, `taskiq` and a `warnings.warn` reach the file and nothing reaches stdout/stderr (checked with `capsys`).
- **Redaction filter:** sensitive `extra` keys are replaced; others pass through.
- **Context:** `bind` sets and restores; values appear on records; they survive `run_in_threadpool` and `asyncio.to_thread`.
- **Middleware:** generates an id when absent, accepts a valid incoming one, rejects an oversized/odd one, sets the response header, logs `route` as the template and never the query string, logs 5xx as ERROR with a traceback.
- **API → worker correlation:** `TaskiqUploadQueue.enqueue` puts the bound request id in the message labels (tested with the in-memory broker, alongside `tests/adapters/test_taskiq_adapters.py`); the task binds it from the labels. The `UploadQueue` port is unchanged; the id travels via context, not a parameter.
- **Events:** one test per flow asserting the event name and key fields — search, login success/failure (and no username on failure), `sync.connection` on success, auth failure and generic failure (gap §7.1), `upload.processed` for `ready` and `failed`.
- **Privacy:** a search for a distinctive string, a sync of a fixture email with a distinctive subject and sender, an upload with a distinctive filename, and a workspace with a distinctive name leave no trace of those strings in captured logs, with the level at `DEBUG`.

## 13. Implementation plan

Small, separately reviewable steps:

1. `observability/` package: context vars, filters, JSON formatter, rotating file handler, taking over library console handlers, `configure_logging`, `timed`. Settings `log_dir`/`log_level`/`log_level_libs`/`log_max_bytes`/`log_backup_count`. Unit tests.
2. Wire setup into the API lifespan, worker startup and reindex script; silence `uvicorn.access`; cap third-party loggers; `app.started`/`worker.ready` timings.
3. Access-log middleware + `X-Request-ID`; `user_id`/`workspace_id` binding in `deps.py`.
4. Request id through Taskiq labels; worker binding; retries keep the id.
5. Events for search, auth, workspaces, sources/sync (closing §7.1), uploads, sweep, reindex.
6. Docker: `/data/logs` in the `Dockerfile`, `findr_logs_data` volume, dev bind mount; `.env.example`; README "Logs" section.

## 14. Implementation notes

What changed from the plan above, or was found while building it:

- **Worker file names include the process index.** `taskiq worker --workers N` runs N processes in one container, all with the same hostname, so `worker-<hostname>.log` would have had several processes rotating one file. Files are `worker-<hostname>-<n>.log` from taskiq's process names (`worker-0`, …). Verified with `--workers 2`: two files.
- **Library console handlers are reclaimed after imports, not just at setup.** `huggingface_hub` and `transformers` attach their own stderr handler when first imported, which happens after `configure_logging` (the models load lazily). `reclaim_console_handlers()` scans every logger and moves any console handler's output to the file. It runs in `configure_logging` and again right after the heavy imports in the embedding adapter and the parser's `warm_up`.
- **Progress bars** (model loading) print straight to stderr, not through logging. `configure_logging` sets `TQDM_DISABLE=1` and `HF_HUB_DISABLE_PROGRESS_BARS=1` (unless already set) before those libraries are imported.
- **More libraries capped.** At the root's default `DEBUG`, `sqlalchemy` would log every SQL statement *with its parameters*, which include filenames and workspace names, so it's capped like the others. Also added: `psycopg`, `redis`, `starlette`, `fastapi`, `anyio`, `watchfiles`, `tzlocal`, `MARKDOWN` and the model/PDF libraries. The privacy test (§12) runs at `DEBUG` to catch any more.
- **What still reaches the console, verified:** uvicorn's first two lines (`Started server process`, `Waiting for application startup`), its reloader's lines in dev, and taskiq's supervisor process (`Starting 1 worker processes`, shutdown lines). None of these processes run our code.
- **Context in FastAPI dependencies** uses `add_fields`, which updates the request's context in place. A `bind` there would be lost: sync dependencies run in a threadpool on a *copy* of the context.
- **`ProcessUpload`'s retry path** keeps the request id: the task re-enqueues with the same label (`tasks._requeue`).
- **`sync.manual` dropped.** A manual resync logs the same `sync.connection` event; its `request_id` ties it to the `POST /sources/{connection_id}/sync` access line. `SyncSource.execute` now returns the status it left the connection in, which the scheduler counts for `sync.tick`.
- **Field renames.** `token_refreshed` → `credentials_refreshed` (the redaction filter would blank any key containing "token"). `workspace.deleted` reports `connections_deleted` and `uploads_deleted` (no document count: the repository doesn't return one).
- **Search and upload timings split by layer.** `search.executed` has `duration_ms`, `hits`, `query_chars`. The adapter's DEBUG `search_index.searched` has `embed_ms`, `es_ms`, `keyword_hits`, `semantic_hits`. `upload.processed` has `parse_ms`, `chunks`, `table_chunks`, `index_ms`, `document_id`. The DEBUG `parser.parsed` has `partition_ms`, `elements`, `pages`, and `search_index.indexed` has `embed_ms`, `bulk_ms`. `ocr_used` wasn't added: `unstructured` doesn't report it.
- **500 responses don't carry `X-Request-ID`.** Starlette's outermost error middleware builds the 500 outside ours. The `http.request.failed` line still has the id and the traceback.
- **The reindex script prints nothing.** Its result is the `reindex.completed` line in `reindex.log`.
- **`upload.processed` for `skipped`** (duplicate or stale message) is DEBUG.
- **Email addresses are masked** (`[email]`) in `msg`, `exc_message` and `stack`. Gmail adapter errors quote Google's response body (`gmail_connector._get`, `gmail_oauth_provider`), which can contain an address, and `SyncSource` now logs those exceptions. Tracebacks are kept for debugging; addresses inside them are masked. Audited: the Gmail adapters' own log lines carry message ids and API paths only, and stored-file paths are `<user_id>/<uuid>.<ext>`, so no filenames.
- **Use cases import only the stdlib helpers.** `findr.observability` re-exports `log_event`, `timed`, `bind`, `add_fields`, `current_fields` and `new_id` (plain standard library). `configure_logging` and `reclaim_console_handlers` live in `findr.observability.setup`, which loads config, and only entry points and adapters import it. Use cases use `log_event` and `bind` as well as `timed`.
- **Known limits:**
  - Two separate `taskiq worker` commands on one host (not one command with `--workers N`) would both write `worker-<host>-0.log`. Docker gives each worker container its own hostname, so this only affects running two worker commands by hand on the host.
  - `data/logs/` ships in the repo (empty, via `data/logs/.gitkeep`). Outside Docker, `configure_logging` creates the folder if it's missing anyway. In Docker, a missing bind-mount folder would be created by Docker, owned by root, and the app would fail to start with "Log directory … isn't writable". Shipping the folder avoids that. On Linux the container's `findr` user (a system uid) may still not match the folder's owner; if startup reports it isn't writable, `chmod a+w data/logs`. macOS Docker Desktop isn't affected.
  - A worker shut down while still starting up can leave its log file half-written. Harmless.
