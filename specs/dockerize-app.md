# Spec: Run the Findr app itself in a Docker container

Status: **Implemented** (`feature/dockerize-app`) — verified with `docker compose up --build`: all three containers healthy, `/healthz`, login, `/sources`, `/search`, and the frontend index route all confirmed working end-to-end against the containerized app.
Owner: findr
Related: `docker-compose.yml` (already runs Postgres + Elasticsearch for local dev; this spec adds the app as a third service in the same file), `specs/postgres-migration.md`, `specs/elasticsearch-search.md`.

## 1. Purpose & scope

Today `docker-compose.yml` only runs the app's two dependencies (Postgres, Elasticsearch) — the FastAPI app itself still runs on the host via `uv run uvicorn ...`. This spec containerizes the app too, so the whole stack (`app` + `postgres` + `elasticsearch`) comes up with one `docker compose up`, with no local Python/uv install required to run it.

### In scope
- A `Dockerfile` for the app (multi-stage: `uv`-based build stage, slim runtime stage).
- Adding an `app` service to the existing `docker-compose.yml`, wired to `postgres`/`elasticsearch` by service name (not `localhost`), with `depends_on: condition: service_healthy` on both.
- A `.dockerignore` (currently missing) so `.venv`, `.git`, `__pycache__`, `.env`, and test artifacts don't bloat the build context.
- Passing secrets/config (`FINDR_SESSION_SECRET`, `FINDR_TOKEN_ENCRYPTION_KEY`, OAuth client credentials, etc.) into the container the same way they already work locally — via `.env` (gitignored), through the compose service's `env_file`.

### Out of scope
- Production hosting / orchestration (Kubernetes, ECS, etc.) — this is a local/dev-parity container, not a production deployment story.
- Multi-worker/horizontal scaling of the app container — out of scope per the existing single-APScheduler-instance constraint (`specs/gmail-connector.md` §8: multiple schedulers would double-sync). One `app` container, one process, no `--workers N`.
- Rewriting `.env.example` — it already lists every variable the container needs; no changes expected.

## 1a. Dev hot-reload (added after initial implementation)

The base `Dockerfile`/`docker-compose.yml` above build a production-style image: code is `COPY`'d in at build time, so an edit requires `docker compose up --build` to see it. For local development that's slower than the host-run workflow (`uv run uvicorn --reload`), so a second file adds reload support without touching the base setup:

**`docker-compose.override.yml`** (new): Docker Compose merges this into `docker-compose.yml` automatically whenever both are present in the same directory — no extra flag needed. It:
- Bind-mounts `./src` over the image's `/app/src`, so host edits are visible inside the running container immediately (no rebuild).
- Overrides the `app` service's `command` to add `--reload --reload-dir /app/src` to the same `uvicorn` invocation. No new dependency: `fastapi[standard]` already pulls in `uvicorn[standard]`, which bundles `watchfiles` (confirmed in the build output) — `--reload` works out of the box.
- Scopes the reload watcher to `/app/src` specifically (not uvicorn's default cwd-wide watch), since `/app` also contains `.venv` and watching that would be both wasteful and pointless (site-packages don't change at dev time).

**Effect on the default workflow**: since the override file auto-merges, a plain `docker compose up` now runs in dev/reload mode by default — matching "the thing you type when developing" to "the thing that reloads." Getting the original production-style, no-reload, no-bind-mount container (e.g. to sanity-check what will actually ship, or in CI) means explicitly excluding the override: `docker compose -f docker-compose.yml up --build`.

**Out of scope, still**: multi-worker/horizontal scaling (unchanged from §1's original scope note — reload mode is still a single `uvicorn` process, just restarted on file change) and any change to the Dockerfile's production build itself.

## 2. Dockerfile design

Multi-stage build, following Astral's documented pattern for `uv`-managed projects:

1. **Builder stage** (`ghcr.io/astral-sh/uv:python3.14-bookworm-slim`): copy `pyproject.toml` + `uv.lock` first and run `uv sync --frozen --no-install-project --no-dev` (installs dependencies into `.venv`, cached as its own layer — only re-runs when lockfile/pyproject change, not on every source edit); then copy `src/` and run `uv sync --frozen --no-dev` to install the project itself into the same `.venv`.
2. **Runtime stage** (`python:3.14-slim-bookworm`, no `uv` needed at runtime): copy the built `.venv` and `src/` from the builder stage, add `.venv/bin` to `PATH`, run as a non-root user, `CMD` runs `uvicorn findr.adapters.inbound.http.app:app --host 0.0.0.0 --port 8000` directly (no `uv run` — the venv is already on `PATH`).

Known risk, to resolve during implementation rather than design: `requires-python = ">=3.14"` is very new — if any dependency (`cryptography`, `argon2-cffi`) lacks a prebuilt wheel for `cp314` on the build platform, `uv sync` falls back to a source build, which may need a C toolchain (and, for `cryptography`, Rust) not present in the slim builder image. If that happens, the fix is adding `build-essential` (and `cargo`/`rustc` if needed) to the **builder** stage only — the runtime stage stays slim either way since it never runs `uv sync`.

## 3. `docker-compose.yml` changes

New `app` service, added to the existing file (not a separate compose file — the point is one `docker compose up` for the whole stack):

```yaml
  app:
    build: .
    ports:
      - "8000:8000"
    env_file:
      - .env
    environment:
      FINDR_DATABASE_URL: postgresql+psycopg://findr:findr@postgres:5432/findr
      FINDR_ELASTICSEARCH_URL: http://elasticsearch:9200
    depends_on:
      postgres:
        condition: service_healthy
      elasticsearch:
        condition: service_healthy
```

- `env_file: .env` picks up everything a developer already has locally (session secret, token encryption key, OAuth client credentials) — same file `.env.example` documents today, no new secrets-handling mechanism introduced.
- The explicit `environment:` block overrides just the two host-pointing URLs from `.env` (which say `localhost`, correct for the app running on the host, wrong for the app running as a container on the compose network) — Docker Compose's own service-name DNS makes `postgres`/`elasticsearch` resolve correctly inside the network.
- `depends_on: condition: service_healthy` reuses the two `healthcheck:` blocks already defined for `postgres`/`elasticsearch` — the app container won't start until both are actually ready, not just started.

## 4. `.dockerignore`

New file, mirroring the relevant parts of `.gitignore` plus build-context-specific exclusions:

```
.venv/
__pycache__/
*.py[oc]
.git/
.env
*.db
*.db-wal
*.db-shm
.pytest_cache/
.DS_Store
```

## 5. Edge cases

| Edge case | Handling |
|---|---|
| Developer has no `.env` yet | `docker compose up` fails fast on a missing `env_file` target — same "you must configure this first" experience `uv run uvicorn` already has locally; not a new failure mode. |
| `findr`/`findr_test` Postgres databases not yet created | Unchanged from today — `findr` (the app's default DB) already exists via `init_db()`'s `CREATE TABLE IF NOT EXISTS`-equivalent; `findr_test` is test-only and irrelevant to running the app container. |
| Static frontend file (`src/findr/Unified Search Interface.html`) | Already served via a path relative to the installed package (`app.py`'s `FINDR_PACKAGE_DIR`); copying `src/` into the image is sufficient, no separate static-file step needed. |
| Rebuilding after a dependency change | Standard Docker layer caching handles it: editing `pyproject.toml`/`uv.lock` invalidates the dependency-install layer; editing only application code does not, so `docker compose up --build` after a code change stays fast. |

## 6. Verification

**Automated**: none — this is infra, not app logic; the existing test suite (`uv run pytest`, run on the host against dockerized Postgres/ES per the prior migration) is unaffected and doesn't need the app itself containerized to pass.

**Manual**: `docker compose up --build`, confirm all three containers report healthy/running, `curl localhost:8000/healthz` returns `{"error": false}`, log in as the seeded demo account, connect/sync/search against the containerized app exactly as verified against the host-run app in `specs/postgres-migration.md` §10 / `specs/elasticsearch-search.md` §11.
