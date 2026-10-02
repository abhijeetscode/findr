# findr

Findr is a unified search platform: connect data sources (Gmail and uploaded files — Google Drive/WhatsApp planned) and search across all of them from one place.

See `specs/` for the design behind each feature (spec-driven development — every feature has a spec written and agreed before implementation).

## Features

- **Workspaces** — one per client. Every Gmail connection and uploaded file belongs to exactly one workspace, and search only looks inside the current one (`specs/workspaces.md`).
- **Gmail** — connect an account via Google OAuth; mail is synced in the background on a schedule and indexed for keyword search (`specs/gmail-connector.md`).
- **File uploads** — PDF, DOCX, TXT and Markdown. Uploads are processed in the background by a worker: OCR for scanned PDFs, table detection, and structure-aware chunking with [Unstructured](https://github.com/Unstructured-IO/unstructured) (`specs/file-upload.md`, `specs/upload-chunking.md`).
- **Search** — BM25 keyword search across everything; uploaded files also get semantic search (local `Qwen/Qwen3-Embedding-0.6B` embeddings, combined with BM25 via reciprocal rank fusion) (`specs/elasticsearch-search.md`, `specs/semantic-search.md`).

## Architecture

FastAPI backend following hexagonal architecture (ports & adapters):

```
src/findr/
  domain/        entities, value objects, exceptions — no framework/I/O
  application/   use cases (auth, workspaces, sources, sync, uploads, search)
  ports/         interfaces the application depends on
  adapters/
    inbound/http/   FastAPI app and routers (serves the UI at /)
    outbound/       Postgres, Elasticsearch, Gmail, embeddings, file storage, crypto, scheduler
    taskiq/         upload queue + worker tasks
  observability/ logging setup, request context, timing helpers
```

Runtime services:

| Service | Role |
|---|---|
| `app` | FastAPI API + UI; runs the Gmail sync scheduler |
| `worker` | Taskiq worker that processes uploads (OCR, chunking, embeddings) |
| `postgres` | System of record |
| `elasticsearch` | Search index (keyword + vectors) |
| `redis` | Upload job queue |

## Requirements

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- [Docker](https://www.docker.com/) — for Postgres, Elasticsearch and Redis, or to run the whole app in containers
- To run the worker outside Docker: `tesseract` and `poppler` (e.g. `brew install tesseract poppler`)

## Setup

```bash
uv sync
```

## Configuration

Copy `.env.example` to `.env` and fill in real values before running:

```bash
cp .env.example .env
```

At minimum, set `FINDR_SESSION_SECRET` and generate a token encryption key for `FINDR_TOKEN_ENCRYPTION_KEY`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

To connect Gmail, create an OAuth client in Google Cloud Console and set `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` (see `specs/gmail-connector.md`, "Prerequisite: Google Cloud setup").

The embedding model (~1.2GB) downloads on first startup. `.env.example` documents the remaining settings (sync interval, embedding model, similarity floor, upload storage path).

## Run

### Option 1: everything in Docker (recommended)

```bash
docker compose up --build
```

Brings up the app, worker, Postgres, Elasticsearch and Redis together at `http://localhost:8000`. `docker-compose.override.yml` is auto-merged, so this runs in **dev mode by default**: code changes under `src/` reload the app automatically, no rebuild needed. The worker doesn't hot-reload — run `docker compose restart worker` after changing its code.

The worker processes one upload at a time and needs ~3-4GB of memory while running. For more throughput, memory permitting:

```bash
docker compose up -d --scale worker=2
```

For the plain production-style containers (no bind mount, no reload):

```bash
docker compose -f docker-compose.yml up --build
```

### Option 2: app on the host, dependencies in Docker

```bash
docker compose up -d postgres elasticsearch redis
```

Set `FINDR_UPLOAD_STORAGE_ROOT` in `.env` to a writable path (e.g. `./data/uploads`), then run the app and the worker in separate terminals:

```bash
uv run uvicorn findr.adapters.inbound.http.app:app --app-dir src --reload
```

```bash
PYTHONPATH=src uv run taskiq worker findr.adapters.taskiq.tasks:broker \
  --workers 1 --max-async-tasks 1 --max-prefetch 1 --ack-type when_executed
```

### Logging in

There's no public sign-up, so log in with the seeded demo account — `demouser` / `password@2050` (override via `FINDR_DEMO_USERNAME`/`FINDR_DEMO_PASSWORD` in `.env`). On first login you'll be asked to create a workspace.

## Reindexing

Postgres is the source of truth. To rebuild the Elasticsearch index from it (fresh environment, Elasticsearch data loss, or after changing the embedding model):

```bash
uv run python scripts/reindex_search.py
```

It prints nothing; the result is the `reindex.completed` line in `data/logs/reindex.log`.

## Logs

Logs go to files only — nothing is printed to the console once the app has started (`specs/logging-telemetry.md`). Each process writes its own file, one JSON object per line, rotated at 10MB with 5 old files kept:

| Process | File |
|---|---|
| API | `data/logs/api.log` |
| Worker | `data/logs/worker-<hostname>.log` (one per worker) |
| Reindex script | `data/logs/reindex.log` |

In dev Docker and on the host the files land in `data/logs/`; the production-style compose file keeps them on the `findr_logs_data` volume. `FINDR_LOG_LEVEL` defaults to `DEBUG` (set `INFO` to quiet it down); see `.env.example` for the other settings.

Every line carries `request_id`, and `user_id`/`workspace_id` where known. An upload's worker lines share the id of the request that uploaded it, so one request can be followed across files. Useful `jq` recipes:

```bash
tail -f data/logs/api.log | jq .                                      # follow the API
jq 'select(.request_id=="<id>")' data/logs/*.log                      # one request, API + worker
jq 'select(.event=="search.executed" and .duration_ms>1000)' data/logs/api.log   # slow searches
jq 'select(.level=="ERROR" or .level=="WARNING")' data/logs/*.log     # problems
```

Every response has an `X-Request-ID` header to search for. Logs never contain search text, file names, workspace names, Gmail addresses or document content. If a container won't start, `docker compose logs <service>` still shows the crash.

## Tests

The suite runs against the real dockerized Postgres and Elasticsearch (no mocks/`:memory:` at the adapter layer); the upload queue is replaced by an in-memory stand-in, so Redis isn't needed. One-time setup — create the test database:

```bash
docker compose up -d postgres elasticsearch
docker compose exec postgres psql -U findr -d findr -c "CREATE DATABASE findr_test;"
```

Then:

```bash
uv run pytest
```
