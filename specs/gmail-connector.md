# Spec: Connect Gmail as a Data Source

Status: **Draft — agreed, not yet implemented**
Owner: findr
Related: `CLAUDE.md` (project goal, hexagonal architecture, spec-driven development)

## 1. Purpose & scope

Let a Findr user connect their Gmail account so their email becomes searchable alongside other connected sources. This is the first source connector built, on a currently empty codebase, so it also establishes the hexagonal (ports & adapters) structure and the multi-user auth baseline that every later connector (Slack, WhatsApp, Google Drive) and later capability (semantic search, file uploads) will build on.

### In scope
- Minimal real user accounts (register/login/logout) with per-user data isolation.
- Connecting a Gmail account via OAuth 2.0 (PKCE), scoped to one Findr user.
- Background sync of Gmail messages (subject, sender, recipients, sent date, plain-text body) into Findr's own SQLite store.
- Keyword (full-text) search over synced email, scoped to the requesting user.
- Handling of token expiry/refresh, revoked access, rate limiting, dedup, and deletions (see §7).

### Out of scope (this feature)
- Slack, WhatsApp, Google Drive connectors, and file uploads — later features. The `SourceConnector` port must accommodate them without rework, but no other connector is built here.
- Semantic/AI/vector search — `SearchIndex` is a separate port from `DocumentRepository` specifically so a vector-index adapter can be added later without touching domain/use-case code.
- Gmail attachments — not fetched, not stored, not indexed.
- Gmail push notifications (Pub/Sub) — polling via a background scheduler is sufficient for MVP.
- Full disk/DB-at-rest encryption — only OAuth tokens are encrypted (column-level, via Fernet); email content is stored as plain SQLite columns. This is a known MVP limitation, not solved here.
- Account deletion / data export (GDPR-style) flows.
- Multi-worker/production process deployment (process supervision, horizontal scaling of the scheduler).

### Why store synced content locally (not query Gmail live per search)
Unified search must merge results across multiple sources with different native search syntaxes and APIs; a single local FTS index gives one consistent ranking/query surface. It also avoids per-search Gmail API quota consumption and latency, keeps search working if Gmail is briefly unreachable or a token needs refresh, and gives the later semantic-search phase something to build embeddings against. This mirrors the "Indexing model" decision already recorded in `CLAUDE.md`.

## 2. Domain model

Plain Python dataclasses in `domain/entities.py` — no framework imports.

```python
@dataclass
class User:
    id: int
    email: str
    password_hash: str
    created_at: datetime

@dataclass
class SourceConnection:
    id: int
    user_id: int
    source_type: SourceType          # value_objects.py enum: GMAIL (extensible)
    external_account: str | None      # the connected Gmail address
    status: ConnectionStatus          # ACTIVE | NEEDS_REAUTH | ERROR | DISCONNECTED
    sync_cursor: str | None           # opaque per-source cursor (Gmail historyId)
    last_synced_at: datetime | None
    last_error: str | None
    created_at: datetime

@dataclass
class Document:
    id: int
    user_id: int
    connection_id: int
    external_id: str                  # Gmail message id
    subject: str | None
    sender: str | None
    recipients: str | None
    body_text: str | None
    sent_at: datetime | None

@dataclass
class SearchHit:
    document: Document
    snippet: str
    score: float
```

`domain/exceptions.py`: `InvalidCredentials`, `DuplicateUser`, `ConnectionNotFound`, `SourceAuthError` (raised when a connector's stored credentials are invalid/expired — the application layer catches this and transitions a connection to `NEEDS_REAUTH`).

## 3. Ports (interfaces)

All in `ports/`, defined as `typing.Protocol` (or ABC), imported and implemented only by `adapters/`. Application-layer use cases depend on these, never on concrete adapters.

```python
class UserRepository(Protocol):
    def get_by_email(self, email: str) -> User | None: ...
    def get_by_id(self, user_id: int) -> User | None: ...
    def create(self, email: str, password_hash: str) -> User: ...

class SessionStore(Protocol):
    def create(self, user_id: int) -> str: ...          # returns session token
    def get_user_id(self, token: str) -> int | None: ...
    def delete(self, token: str) -> None: ...

class PasswordHasher(Protocol):
    def hash(self, plaintext: str) -> str: ...
    def verify(self, plaintext: str, hashed: str) -> bool: ...

class SourceConnectionRepository(Protocol):
    def create(self, user_id: int, source_type: SourceType) -> SourceConnection: ...
    def get(self, connection_id: int, user_id: int) -> SourceConnection | None: ...
    def list_for_user(self, user_id: int) -> list[SourceConnection]: ...
    def update_status(self, connection_id: int, status: ConnectionStatus, last_error: str | None = None) -> None: ...
    def update_cursor(self, connection_id: int, cursor: str, synced_at: datetime) -> None: ...

class CredentialStore(Protocol):
    def save(self, connection_id: int, access_token: str, refresh_token: str, expires_at: datetime) -> None: ...
    def get(self, connection_id: int) -> Credentials | None: ...   # decrypted at read time
    def delete(self, connection_id: int) -> None: ...

class OAuthProvider(Protocol):
    def build_authorize_url(self, state: str, code_challenge: str) -> str: ...
    def exchange_code(self, code: str, code_verifier: str) -> Credentials: ...
    def refresh(self, refresh_token: str) -> Credentials: ...
    def revoke(self, token: str) -> None: ...

class ChangeBatch:
    upserts: list[Document]
    deleted_external_ids: list[str]
    new_cursor: str

class SourceConnector(Protocol):
    def fetch_changes(self, credentials: Credentials, cursor: str | None) -> ChangeBatch: ...

class DocumentRepository(Protocol):
    def upsert_many(self, documents: list[Document]) -> None: ...
    def delete_many(self, connection_id: int, external_ids: list[str]) -> None: ...

class SearchIndex(Protocol):
    def search(self, user_id: int, query: str) -> list[SearchHit]: ...

class Clock(Protocol):
    def now(self) -> datetime: ...
```

## 4. Application use cases

One per file under `application/`, each a small class/function constructor-injected with only the ports it needs (no framework types in signatures).

- `auth/register_user.py` — `RegisterUser(user_repo, hasher).execute(email, password) -> User`, raises `DuplicateUser` if email taken.
- `auth/login_user.py` — `LoginUser(user_repo, hasher, session_store).execute(email, password) -> str (session token)`, raises `InvalidCredentials`.
- `auth/logout_user.py` — `LogoutUser(session_store).execute(token)`.
- `sources/connect_gmail.py` — `BeginGmailConnect(oauth_provider, connection_repo, oauth_state_repo).execute(user_id) -> authorize_url`; `CompleteGmailConnect(oauth_provider, connection_repo, credential_store, oauth_state_repo).execute(code, state) -> SourceConnection`.
- `sources/list_connections.py` — `ListConnections(connection_repo).execute(user_id) -> list[SourceConnection]`.
- `sources/disconnect_source.py` — `DisconnectSource(connection_repo, credential_store, oauth_provider).execute(connection_id, user_id)` — revokes token, deletes credentials, marks connection `DISCONNECTED`.
- `sync/sync_source.py` — `SyncSource(connector, credential_store, connection_repo, document_repo).execute(connection: SourceConnection)` — generic over `SourceConnector`, works for Gmail now and any future connector unchanged. Catches `SourceAuthError` → sets `NEEDS_REAUTH`; catches other exceptions → sets `ERROR` with `last_error`, without raising (isolates one connection's failure from the sync loop).
- `search/search_documents.py` — `SearchDocuments(search_index).execute(user_id, query) -> list[SearchHit]`.

## 5. Auth design

- **Scheme**: email + password, Argon2 password hashing (`argon2-cffi`), server-side session token in an `HttpOnly`, `SameSite=Lax` cookie (`Secure` in production). Not JWT — the Gmail OAuth callback is a top-level browser redirect from Google, so no `Authorization` header survives that hop; identity must travel as a cookie. Sessions are also trivially revocable once tokens are involved.
- **`SameSite=Lax`, not `Strict`**: `Strict` would drop the session cookie on the top-level redirect back from Google, breaking the callback.
- **`oauth_states`**: `(state, user_id, code_verifier, expires_at)`, created at `BeginGmailConnect`, validated at `CompleteGmailConnect`. The resulting connection is associated with `oauth_states.user_id` — the user who *initiated* the connect — not whatever session happens to be current when the callback lands. Closes a session-fixation-style gap.
- **Ownership**: `source_connections.user_id` is a required FK; every repository method that reads/writes connections or documents takes and filters by `user_id` explicitly — never inferred implicitly inside an adapter.

## 6. Gmail OAuth flow

**Library**: raw OAuth 2.0 + PKCE over `httpx` (async), not `google-auth-oauthlib`/`google-api-python-client` (both synchronous, would fight FastAPI's async model, and have known local-dev PKCE/redirect pitfalls). The needed Gmail surface is small: authorize URL, token exchange, token refresh, `users.messages.list`, `users.messages.get`, `users.history.list`.

**Scopes**: `https://www.googleapis.com/auth/gmail.readonly`, `openid`, `email`. `gmail.readonly` is a restricted scope, so the OAuth consent screen stays in Google's "Testing" mode for MVP (see §9) — the developer's own account must be added as a test user.

**Authorize request**: include `access_type=offline` and `prompt=consent` — without both, a reconnect can silently omit the `refresh_token`, breaking background sync.

**Endpoints** (`adapters/inbound/http/routers/sources_router.py`):
- `GET /sources/gmail/connect` — requires an authenticated session. Creates an `oauth_states` row, returns **HTTP 302** straight to Google's authorization URL (a redirect, not a JSON body — must be directly browsable).
- `GET /sources/gmail/callback?code=&state=` — validates `state`/expiry, exchanges `code` (+ PKCE verifier) for tokens, encrypts and stores them, upserts the `source_connections` row (`status=ACTIVE`), redirects to `/sources`.
- `GET /sources` — list the current user's connections + status.
- `DELETE /sources/{id}` — revokes the token at Google, deletes stored credentials, marks the connection `DISCONNECTED`.

**Token storage**: access/refresh tokens encrypted at rest with Fernet (`cryptography`), keyed from an env secret (`FINDR_TOKEN_ENCRYPTION_KEY`). Column-level encryption only — see the out-of-scope note in §1.

**Refresh-token expiry is routine, not exceptional**: in Google's Testing publishing status, refresh tokens for restricted scopes expire after **7 days**. A refresh failure during sync sets `status=NEEDS_REAUTH` and stops further sync attempts for that connection until the user reconnects (re-running the connect flow overwrites the existing connection's tokens rather than requiring disconnect-then-reconnect).

## 7. Data model (SQLite)

```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE sessions (
    token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);

CREATE TABLE oauth_states (
    state TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    code_verifier TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);

CREATE TABLE source_connections (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    source_type TEXT NOT NULL,             -- 'gmail'
    external_account TEXT,
    status TEXT NOT NULL,                  -- active | needs_reauth | error | disconnected
    access_token_enc BLOB, refresh_token_enc BLOB, token_expires_at TEXT,
    sync_cursor TEXT,                      -- Gmail historyId
    last_synced_at TEXT, last_error TEXT, created_at TEXT NOT NULL,
    UNIQUE(user_id, source_type, external_account)
);

CREATE TABLE documents (
    id INTEGER PRIMARY KEY,                -- required: FTS5 external-content needs an integer rowid
    user_id INTEGER NOT NULL REFERENCES users(id),
    connection_id INTEGER NOT NULL REFERENCES source_connections(id),
    external_id TEXT NOT NULL,             -- Gmail message id
    subject TEXT, sender TEXT, recipients TEXT, body_text TEXT, sent_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(connection_id, external_id)     -- dedup key across re-syncs
);

CREATE VIRTUAL TABLE documents_fts USING fts5(
    subject, sender, body_text, content='documents', content_rowid='id'
);

-- Triggers keep FTS in sync. INSERT ... ON CONFLICT DO UPDATE must be used
-- for upserts (never INSERT OR REPLACE) so these DELETE/INSERT pairs fire
-- correctly and the FTS index never goes stale.
CREATE TRIGGER documents_ai AFTER INSERT ON documents BEGIN
  INSERT INTO documents_fts(rowid, subject, sender, body_text)
  VALUES (new.id, new.subject, new.sender, new.body_text);
END;
CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN
  INSERT INTO documents_fts(documents_fts, rowid, subject, sender, body_text)
  VALUES ('delete', old.id, old.subject, old.sender, old.body_text);
END;
CREATE TRIGGER documents_au AFTER UPDATE ON documents BEGIN
  INSERT INTO documents_fts(documents_fts, rowid, subject, sender, body_text)
  VALUES ('delete', old.id, old.subject, old.sender, old.body_text);
  INSERT INTO documents_fts(rowid, subject, sender, body_text)
  VALUES (new.id, new.subject, new.sender, new.body_text);
END;
```

**Per-user isolation**: `user_id` is deliberately kept out of `documents_fts`. Every search joins back to `documents` and filters `WHERE d.user_id = :user_id`, so isolation can't be bypassed via the `MATCH` string:

```sql
SELECT d.* FROM documents_fts f JOIN documents d ON d.id = f.rowid
WHERE documents_fts MATCH :query AND d.user_id = :user_id ORDER BY rank;
```

User search input must have FTS5 special characters (`"`, `-`, `:`, `*`) escaped/quoted per token before being passed to `MATCH`.

**Connection hygiene**: WAL mode, `PRAGMA busy_timeout=5000` — relevant once the sync scheduler runs alongside request handling.

## 8. Background sync

- **Mechanism**: APScheduler (`apscheduler>=3.10,<4`) running inside the FastAPI process, started/stopped via `lifespan` in `adapters/inbound/http/app.py`. No separate worker process for MVP.
- **MVP constraint**: single worker, no `--reload` — multiple workers would start multiple schedulers and double-sync. `max_instances=1, coalesce=True` per job limits damage if a tick overlaps.
- **Trigger**: a tick job every `FINDR_SYNC_INTERVAL_SECONDS` (default 300s) iterates all `source_connections` with `status=ACTIVE` and calls `SyncSource` for each, independently try/excepted so one account's failure never blocks another's.
- **Initial sync**: capture the current `historyId` (via `users.getProfile`) *before* listing begins, so mail arriving mid-sync isn't missed; paginate `users.messages.list` with checkpointed `pageToken`; fetch each message via `users.messages.get`.
- **Incremental sync**: `users.history.list` with the stored `sync_cursor`. Process `messagesAdded` (upsert), `messagesDeleted` and `TRASH`/`SPAM` label additions (treat as deletion from the index). Update `sync_cursor` to the new `historyId` on success. A 404 (history too old — Gmail retains ~7 days) triggers a full resync.
- **Normalization**: `gmail_mime.py` extracts subject/sender/recipients/sent date/plain-text body from the message payload (prefer `text/plain`, fall back to stripped `text/html`). Attachments are not touched.
- **Rate limiting**: exponential backoff with capped retries on Gmail 429/5xx (`tenacity`), honoring `Retry-After` when present.

## 9. Prerequisite: Google Cloud setup (manual, done once by the developer)

1. Create a Google Cloud project; enable the **Gmail API**.
2. **OAuth consent screen**: External user type; scopes `gmail.readonly` + `openid` + `email`; publishing status **Testing**; add the developer's Google account under **Test users** (required, or Google blocks the flow for anyone not listed).
3. **Credentials → Create OAuth client ID**, type **Web application**, redirect URI exactly `http://localhost:8000/sources/gmail/callback`.
4. Put Client ID/Secret into a local `.env` (never committed) as `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`.
5. Expect reconnects roughly every 7 days during development (Testing-mode refresh token expiry) — handled by the `NEEDS_REAUTH` path, not a bug.

## 10. Edge cases

| Edge case | Handling |
|---|---|
| Token expiry / refresh failure | `SyncSource` catches it, sets `status=NEEDS_REAUTH`, stops syncing that connection until user reconnects |
| 7-day Testing-mode refresh token expiry | Same `NEEDS_REAUTH` path; expected routine occurrence, documented for the user |
| Revoked access (user revokes on Google's side) | Surfaces as a refresh/API 401 → same `NEEDS_REAUTH` path |
| Rate limiting / backoff | Exponential backoff, capped retries, honors `Retry-After`; isolated per connection |
| Duplicate email dedup across re-syncs | `UNIQUE(connection_id, external_id)` + `ON CONFLICT DO UPDATE` (not `INSERT OR REPLACE`, to keep FTS triggers correct) |
| Large mailbox initial sync | Paginated `messages.list` with checkpointed `pageToken`; `historyId` captured before listing starts |
| Partial sync failure isolation | Each connection's sync wrapped independently in the scheduler tick |
| Email deletion (Trash/Spam/delete) | `history.list` deletions/label changes remove the `documents` row, cascading via the DELETE trigger to `documents_fts` |
| Attachments | Out of scope — never fetched or indexed |
| Multi-worker / `--reload` double-scheduling | Documented single-worker MVP constraint; `max_instances=1` limits damage |

## 11. Open follow-ups (not decided here)

- Hard-delete vs. soft-delete of `documents` when a connection is disconnected (current plan: hard-delete tokens always; document retention on disconnect is undecided — default to hard-delete unless told otherwise).
- Whether/how to bound `body_text` size for very large emails.
- Whether to add a retention/expiry policy for synced content (currently: keep indefinitely until disconnected).
- UI wiring into the existing static `Unified Search Interface.html` mockup — explicitly deferred to a later step.

## 12. Verification

**Automated** (`pytest`, in-memory SQLite fixture, no real Gmail credentials needed):
1. Two users search the same keyword → each sees only their own documents.
2. `SyncSource` driven by a `FakeConnector` test double → dedup on re-sync, deletion removes from search.
3. `gmail_mime.py` normalization against a fixture multipart Gmail message JSON.

**Manual end-to-end**: register/login → connect Gmail via browser OAuth → wait for a sync tick → search returns results scoped to that user → a second, unconnected user's search returns nothing from the first user's mail → simulating a bad refresh token surfaces `NEEDS_REAUTH` without crashing the scheduler.
