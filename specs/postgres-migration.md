# Spec: Migrate Persistence from SQLite to PostgreSQL

Status: **Implemented** (`feature/postgres-elasticsearch-migration`) — see the "Deviation found during implementation" notes below for where the build diverged from this spec's original text.
Owner: findr
Related: `CLAUDE.md` (hexagonal architecture), `specs/elasticsearch-search.md` (sibling spec — search moves off SQLite in the same cutover, see §1 for why these two aren't independent), `specs/gmail-thread-id.md` (a prerequisite — its `thread_id` column belongs in this spec's initial schema, see §5), all three connector specs (their "Data model (SQLite)" sections describe the schema this spec carries over unchanged).

## 1. Purpose & scope

Replace SQLite — the adapter behind every persistence port in this app — with PostgreSQL as the system of record, without changing `domain/`, `application/`, or `ports/`. Every place SQLite is touched today is already hidden behind a port (`UserRepository`, `SessionStore`, `OAuthStateRepository`, `SourceConnectionRepository`, `CredentialStore`, `DocumentRepository`); this migration is "write six new adapters," not a rewrite.

**Why this and the Elasticsearch migration land together, not independently**: `SearchIndexSqlite` (today) queries SQLite's `documents_fts` joined directly against the `documents` table. The moment `documents` moves to Postgres, that query breaks — there is no working "half-migrated" state, and no interim Postgres-native full-text search is being built as a bridge (out of scope, see below). So while the two specs describe independent adapters, the actual cutover is one event: both land together, verified together, deployed together.

### In scope
- New Postgres-backed adapters for all six persistence ports, replacing `adapters/outbound/sqlite/*`.
- New `db.py`-equivalent engine/session setup for Postgres (connection pooling, not SQLite pragmas).
- `docker-compose.yml` providing local Postgres (and, per the sibling spec, Elasticsearch) for dev and CI.
- Adapter-level tests (`tests/adapters/*`) running against a real, dockerized Postgres instead of SQLite `:memory:`.
- Config/env changes (`FINDR_DATABASE_URL` replacing `FINDR_DATABASE_PATH`).
- Deleting `adapters/outbound/sqlite/`, `schema.sql`, and SQLite-specific test fixtures once the cutover is verified working end-to-end.

### Out of scope
- Search itself — covered by `specs/elasticsearch-search.md`. This spec's `DocumentRepository` is still the system-of-record write path; it does not talk to Elasticsearch.
- **Migrating existing local SQLite data.** Decided: fresh start. After cutover, reconnect Gmail/Slack/Notion (Slack/Notion since removed — see `specs/remove-slack-notion.md`) and let the normal sync flow repopulate Postgres from scratch. This is still demo/dev-stage data, not production data worth writing a backfill script for.
- Production hosting choice (managed Postgres provider, backups, HA) — not decided here; this spec only requires a `FINDR_DATABASE_URL` pointing at *some* reachable Postgres.
- Any change to `ports/*` (no port signatures change in this spec — `SearchIndex`'s write methods are added in the sibling spec, not here).

## 2. Config changes

- `FINDR_DATABASE_PATH` (a file path) is replaced outright by `FINDR_DATABASE_URL` (a full SQLAlchemy connection URL), e.g. `postgresql+psycopg://findr:findr@localhost:5432/findr`. No dual-support fallback — this is a clean rename, consistent with the agreed fresh-start (no in-place upgrade path needs preserving).
- New dependency: `psycopg[binary]` (psycopg3) — SQLAlchemy 2.0 has first-class support for it and it's the currently-recommended driver for new Postgres projects (vs. the older psycopg2).
- `.env.example`: `FINDR_DATABASE_PATH=findr.db` → `FINDR_DATABASE_URL=postgresql+psycopg://findr:findr@localhost:5432/findr`, with a comment pointing at the new `docker-compose.yml` for how to get a local Postgres matching those credentials.

## 3. `docker-compose.yml`

New file at the repo root:

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: findr
      POSTGRES_PASSWORD: findr
      POSTGRES_DB: findr
    ports:
      - "5432:5432"
    volumes:
      - findr_postgres_data:/var/lib/postgresql/data
  # elasticsearch service added by specs/elasticsearch-search.md

volumes:
  findr_postgres_data:
```

A second database (`findr_test`) is created for the test suite (see §7) — either via a `POSTGRES_MULTIPLE_DATABASES`-style init script, or simply `CREATE DATABASE findr_test;` run once manually/in a Makefile target. Decided later during implementation; not a design fork worth blocking the spec on.

## 4. `db.py` changes

- `create_db_engine(database_url: str) -> Engine`: becomes a plain `create_engine(database_url, pool_pre_ping=True)`. `pool_pre_ping` guards against stale pooled connections (Postgres will close idle connections server-side; SQLite never had this failure mode). No more `:memory:` special-casing, no `StaticPool`, no `check_same_thread`.
- The `_set_pragmas` event listener (`PRAGMA foreign_keys`, `journal_mode=WAL`, `busy_timeout`) is deleted entirely — these are SQLite-specific. Postgres enforces foreign keys by default and handles concurrent writers natively (no WAL-mode equivalent needed); connection-pool tuning, if ever needed, is done via `create_engine(..., pool_size=..., max_overflow=...)` kwargs instead.
- `init_db(engine)`: `Base.metadata.create_all(engine)` already works unchanged — SQLAlchemy generates dialect-appropriate DDL automatically. The `schema.sql` raw-SQL step (FTS5 virtual table + sync triggers) is deleted — that file's entire reason to exist goes away with SQLite (Elasticsearch, not a Postgres trigger, keeps the search index in sync — see the sibling spec's §6 for exactly where that happens instead).

## 5. Data model changes

- `models.py`: `DocumentModel.id`'s existing comment/constraint — *"Plain INTEGER PRIMARY KEY (== SQLite rowid) is required: `documents_fts` ... requires this id to line up with SQLite's own rowid"* — is deleted. That requirement was only ever about satisfying SQLite FTS5's `content_rowid` linkage; it's meaningless once search moves to Elasticsearch, and Postgres's own `SERIAL`/`IDENTITY` primary key works exactly like any other column here (no rowid concept to align with).
- All other column types (`Mapped[bytes | None]` for `access_token_enc`/`refresh_token_enc`, `Mapped[datetime]`, `Mapped[str | None]`) translate automatically via SQLAlchemy's dialect-independent type system — `bytes` → `BYTEA`, `datetime` → `TIMESTAMP`. No adapter-level encryption logic changes; `TokenCipher`/Fernet is already byte-in-byte-out and dialect-agnostic.
- Side benefit, not a requirement of this spec: Postgres's native `TIMESTAMP` type round-trips real Python `datetime` objects even through raw `text()` SQL (unlike SQLite, whose text-affinity storage is what caused the `search_index_sqlite.py` `sent_at` bug fixed earlier this project). Not exercised here since document search moves to Elasticsearch, but worth knowing if any future raw-SQL Postgres query is ever written.
- **Prerequisite from `specs/gmail-thread-id.md`**: `DocumentModel` also gains `thread_id: Mapped[str | None] = mapped_column(index=True)` — Gmail's conversation-grouping key, captured from day one so it doesn't need a migration onto a live table later. See that spec for the full reasoning; it's included here because this is where the schema is actually first created.

## 6. Adapters to rewrite

All six are a SQLAlchemy dialect swap with **no query-logic changes**, except where called out:

| File | Change |
|---|---|
| `user_repository_postgres.py` | Direct port of `user_repository_sqlite.py` — uses plain `select()`/`.get()`, already dialect-agnostic. |
| `session_store_postgres.py` | Same — no dialect-specific SQL. |
| `oauth_state_repository_postgres.py` | Same. |
| `source_connection_repo_postgres.py` | Same. |
| `credential_store_postgres.py` | Same — Fernet encryption is dialect-independent. |
| `document_repository_postgres.py` | **Real change, two parts**: (1) `upsert_many()` currently uses `sqlalchemy.dialects.sqlite.insert` for `ON CONFLICT DO UPDATE`. Postgres has the identical `.on_conflict_do_update(index_elements=..., set_=...)` API via `sqlalchemy.dialects.postgresql.insert` instead — same shape, different import, same `(connection_id, external_id)` conflict target. (2) `upsert_many()`'s signature changes from `-> None` to `-> list[Document]`, adding `.returning(DocumentModel.id)` to the statement and assigning the result back onto each `Document` before returning. This second change isn't a Postgres-dialect concern on its own — it's required by `specs/elasticsearch-search.md` §2/§3.1, which needs each document's real assigned id (not its `id=0` placeholder) to index it into Elasticsearch under the right `_id`. Called out here since this file is where it's actually implemented. |

## 7. Testing strategy

**Application-layer tests are untouched.** `SyncSource`, `connect_gmail` (and, at the time, `connect_slack`/`connect_notion`, since removed — see `specs/remove-slack-notion.md`), `register_and_login`, etc. test against ports via hand-written fakes (`FakeSourceConnectionRepository`, `FakeCredentialStore`, ...), never against a concrete SQLite adapter. Nothing here changes for them.

**Adapter-level tests (`tests/adapters/*`) run against a real, dockerized Postgres** — decided explicitly over mocking, since the entire point of an adapter test is verifying real SQL against the real engine (the `document_repository_postgres.py` `ON CONFLICT` rewrite above is exactly the kind of bug this class of test exists to catch).

- `conftest.py`'s `db_session` fixture changes from "create a fresh `:memory:` engine per test" (free isolation) to a **transaction-per-test** pattern against a shared test database (`findr_test`, on the same docker-compose Postgres): open a connection, begin a transaction, bind the session to it, run the test, roll back — the test's writes never persist past it, giving the same per-test isolation SQLite's `:memory:` gave for free. This is a standard, well-documented SQLAlchemy testing pattern, not a new invention. Concretely, `Session(bind=connection, join_transaction_mode="create_savepoint")`: adapter code under test still calls `session.commit()` freely, but each commit only closes a `SAVEPOINT` nested inside the outer transaction, so the final rollback still discards everything.
- CI needs Postgres available before running `pytest` — either a service container (GitHub Actions `services:` block) or `docker compose up -d postgres` as a pre-test step. Exact CI wiring is an implementation detail, not a design fork.

**Deviation found during implementation**: the router-level tests (`test_auth_router.py`, `test_search_router.py`, `test_sources_router.py`) boot the whole app via `TestClient(app)`, whose `lifespan` builds its *own* engine/session_factory from `FINDR_DATABASE_URL` — a separate connection from whatever `db_session`'s single wrapped connection holds. The transaction-per-test trick doesn't reach them: a second, independent engine can't see an uncommitted transaction sitting on a different connection. These tests instead use a new `app_env` fixture that (a) points `FINDR_DATABASE_URL`/`FINDR_ELASTICSEARCH_INDEX` at the shared `findr_test` database and a freshly-created, uniquely-named Elasticsearch index, and (b) truncates every Postgres table both before and after the test (`TRUNCATE ... RESTART IDENTITY CASCADE`), since anything committed through the app's own engine is a real commit outside any test-scoped transaction and would otherwise leak into whichever test runs next. `db_session` (savepoint rollback) and `app_env` (truncate) are two different isolation mechanisms for two different classes of test, not redundant — a test can't get app-level isolation from a transaction it isn't inside.

## 8. Edge cases

| Edge case | Handling |
|---|---|
| Postgres unreachable at app startup | `create_engine` itself doesn't connect eagerly; the first real query fails loudly (connection refused) — same "fail fast, don't silently degrade" behavior SQLite file-permission errors already had. |
| Stale pooled connection (Postgres closed it server-side after idle timeout) | `pool_pre_ping=True` detects and transparently reconnects before handing a connection to a session. |
| Concurrent writers (multiple app instances) | No longer a documented MVP constraint — this was SQLite's single-writer limitation specifically (see `gmail-connector.md` §8's "single worker" note); Postgres handles concurrent writers natively. This *doesn't* remove the separate "multiple APScheduler instances double-syncing" constraint, which is about the scheduler's own design, not the database. |
| Existing local `findr.db` data | Left in place, untouched, simply no longer read after cutover — not deleted by this migration (the developer can remove it manually; nothing in this spec automates that). |

## 9. Open follow-ups (not decided here)

- Production Postgres hosting (managed service vs. self-hosted) — needs a `FINDR_DATABASE_URL` at deploy time; provider choice deferred.
- Connection pool sizing tuning (`pool_size`, `max_overflow`) — defaults are fine at current (single-demo-user) scale; revisit under real concurrent load.
- Whether `findr_test`'s creation should be automated (docker-compose init script) vs. a one-time manual step — implementation detail, not a design decision.

## 10. Verification

**Automated**: existing adapter test suite (`tests/adapters/test_sqlite_repositories.py`, renamed to `test_postgres_repositories.py`, and friends) passes against the dockerized Postgres, including the `document_repository_postgres.py` upsert-on-conflict behavior specifically.

**Manual end-to-end**: `docker compose up -d postgres`, set `FINDR_DATABASE_URL` in `.env`, run the app, confirm `init_db()` creates all tables, register/log in (well — log in as the seeded demo account), connect a source, confirm a sync writes rows into Postgres (`psql` or any client), disconnect, reconnect — confirm the same dedup/reconnect behavior already verified against SQLite still holds.
