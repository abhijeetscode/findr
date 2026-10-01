# findr

Findr is a unified search platform: connect data sources (Gmail and uploaded files — Google Drive/WhatsApp planned) and search across all of them from one place. FastAPI backend (hexagonal/ports & adapters), Postgres as the system of record, Elasticsearch for search.

See `specs/` for the design behind each feature (spec-driven development — every feature has a spec written and agreed before implementation).

## Requirements

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- [Docker](https://www.docker.com/) — for Postgres + Elasticsearch, or to run the whole app in containers

## Setup

```bash
uv sync
```

## Configuration

Copy `.env.example` to `.env` and fill in real values before running:

```bash
cp .env.example .env
```

At minimum, generate a token encryption key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Run

### Option 1: everything in Docker (recommended)

```bash
docker compose up --build
```

Brings up the app, Postgres, and Elasticsearch together at `http://localhost:8000`. `docker-compose.override.yml` is auto-merged, so this runs in **dev mode by default**: code changes under `src/` reload automatically, no rebuild needed.

For the plain production-style container (no bind mount, no reload):

```bash
docker compose -f docker-compose.yml up --build
```

### Option 2: app on the host, dependencies in Docker

```bash
docker compose up -d postgres elasticsearch
uv run uvicorn findr.adapters.inbound.http.app:app --app-dir src --reload
```

Either way: there's no public sign-up, so log in with the seeded demo account — `demouser` / `password@2050` (override via `FINDR_DEMO_USERNAME`/`FINDR_DEMO_PASSWORD` in `.env`).

## Tests

The suite runs against the real dockerized Postgres and Elasticsearch (no mocks/`:memory:` at the adapter layer). One-time setup — create the test database:

```bash
docker compose up -d postgres elasticsearch
docker compose exec postgres psql -U findr -d findr -c "CREATE DATABASE findr_test;"
```

Then:

```bash
uv run pytest
```
