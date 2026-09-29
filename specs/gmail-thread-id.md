# Spec: Capture Gmail Thread ID (Conversation Grouping)

Status: **Draft — not yet agreed, not implemented.**
Owner: findr
Related: `specs/gmail-connector.md` (the `Document` entity and `gmail_mime.py` this spec extends), `specs/postgres-migration.md` and `specs/elasticsearch-search.md` (**this spec is a prerequisite of both** — see §1).

## 1. Purpose & scope

Today, every synced email is stored as a fully independent row — a reply carries no link back to the message it replies to, or to the rest of its conversation. Gmail's API already gives us `threadId` (a conversation-grouping key) on every message we fetch, at zero extra cost — we simply discard it. This spec captures it.

**Why this is a prerequisite of `postgres-migration.md` and `elasticsearch-search.md`, not a follow-on to them**: both of those specs design the `documents` table/index from scratch. Adding a column to a table that doesn't exist yet is free — one line in `models.py` before it's ever created. Adding it after Postgres is live means a real `ALTER TABLE` plus backfilling every already-synced document (which, per the agreed "fresh start" migration decision, won't have a value to backfill *from* anyway — old data isn't being migrated). Landing this spec's schema change *inside* `postgres-migration.md`'s first `CREATE TABLE`, not after it, avoids that entirely.

### In scope
- `thread_id: str | None` added to the `Document` domain entity.
- Parsing Gmail's `threadId` in `gmail_mime.py` / `gmail_connector.py`.
- The `documents.thread_id` column in the Postgres schema `postgres-migration.md` is designing (indexed).
- The `thread_id` field in the Elasticsearch mapping `elasticsearch-search.md` is designing.

### Out of scope (explicitly, per discussion)
- **Any feature that *uses* `thread_id`** — grouping/collapsing search results by conversation (Elasticsearch's `collapse` query is the natural mechanism, if this is ever built), a "view whole conversation" UI. There is no current product requirement driving either; this spec captures the data because it's free right now, not because a feature needs it today. Building the feature is separate, future scope.
- **Slack (`thread_ts`) and Notion (page hierarchy)** — explicitly out of scope per this conversation. Both have a conceptually similar but structurally different "grouping" notion (Slack: flat, like Gmail; Notion: a real parent-page tree, not a conversation) and are deferred to their own future spec if/when this gets extended past Gmail.
- **`In-Reply-To`/`References` headers** (the exact reply-to pointer and full ancestor chain, vs. `threadId`'s flat grouping) — see §3 for why `threadId` alone is judged sufficient for now.
- **Stripping quoted reply text from `body_text`** — a related but distinct search-quality issue raised in the same discussion (replies quote prior messages, skewing BM25 term statistics across a thread). Not solved here — see `specs/gmail-quote-stripping.md`.
- Does **not** affect BM25 ranking itself — `thread_id` is a grouping key, not a relevance signal. See §1's discussion in the conversation that produced this spec for the full reasoning.

## 2. Domain model change

```python
@dataclass
class Document:
    id: int
    user_id: int
    connection_id: int
    external_id: str
    subject: str | None
    sender: str | None
    recipients: str | None
    body_text: str | None
    sent_at: datetime | None
    thread_id: str | None = None   # new
```

Nullable and defaulted: `Document` is shared across all three connectors, and Slack/Notion don't populate it (out of scope, §1). For Gmail specifically, it's effectively always present (§4) — the field is nullable at the type level because the entity is generic, not because Gmail ever omits it.

## 3. Why `threadId`, not `In-Reply-To`/`References`

Gmail exposes both (see the conversation this spec came from for the full API shapes):

- **`threadId`** — a top-level field on every message resource, computed by Gmail itself. Flat: every message in a conversation shares one value. Always present, robust even when standard mail headers are missing or malformed, since Gmail computes it server-side from its own heuristics (subject, participants, references) rather than relying solely on the headers being well-formed.
- **`In-Reply-To`/`References`** — standard RFC 5322 headers inside `payload.headers`. Precise: names the *exact* message being replied to, and the full ancestor chain in order. Can be missing or incomplete (some clients don't set them correctly; the first message in a thread has no `In-Reply-To` at all).

**Decision: capture `threadId` only.** It's simpler (one top-level field vs. parsing and validating header chains), more reliable (Gmail-computed, not dependent on the sending client having set headers correctly), and sufficient for the only thing currently justifying this work — knowing which messages belong to the same conversation. Reconstructing exact reply order/structure within a thread is a real but *different* capability, with no current requirement driving it — revisit via `In-Reply-To`/`References` if that need ever materializes (§7).

## 4. Gmail-specific parsing

`threadId` is a **top-level field on the message resource** (a sibling of `id` and `payload`), not inside `payload.headers` — so it needs direct dict access, not the existing `_header()` helper:

```json
{
  "id": "18f2a3b1c9d4e5f6",
  "threadId": "18f2a3b1c9d4e5f6",
  "payload": { "headers": [ ... ] }
}
```

`gmail_mime.py` changes:

```python
@dataclass
class ParsedMessage:
    external_id: str
    subject: str | None
    sender: str | None
    recipients: str | None
    body_text: str
    sent_at: datetime | None
    thread_id: str          # new — always present on a Gmail message resource

def parse_gmail_message(message: dict[str, Any]) -> ParsedMessage:
    payload = message.get("payload", {}) or {}
    headers = payload.get("headers", []) or []
    return ParsedMessage(
        external_id=message["id"],
        subject=_header(headers, "Subject"),
        sender=_header(headers, "From"),
        recipients=_header(headers, "To"),
        body_text=_extract_body_text(payload),
        sent_at=_parse_date(_header(headers, "Date")),
        thread_id=message["threadId"],   # new
    )
```

`gmail_connector.py`'s `_fetch_document()` passes `thread_id=parsed.thread_id` into the `Document(...)` it constructs. **No new API call** — `_fetch_document` already calls `messages.get(format=full)` for every message (both initial and incremental sync), and `threadId` is already present in that response; it's simply being discarded today.

## 5. Postgres schema (prerequisite for `postgres-migration.md`)

`DocumentModel` (in `postgres-migration.md`'s schema, i.e. `models.py`) gains:

```python
thread_id: Mapped[str | None] = mapped_column(index=True)
```

Indexed from the start — a future "fetch the whole conversation" query (`WHERE thread_id = ... AND user_id = ...`) is the entire reason this data is worth capturing, and adding an index to an existing populated column later is a heavier operation than including it in the initial `CREATE TABLE`.

## 6. Elasticsearch mapping (prerequisite for `elasticsearch-search.md`)

`elasticsearch-search.md` §4's mapping gains:

```json
"thread_id": { "type": "keyword" }
```

`keyword`, not `text` — it's an exact-match grouping key (used for filtering/collapsing if that feature is ever built), never full-text searched.

## 7. Edge cases

| Edge case | Handling |
|---|---|
| A standalone email with no replies | Gmail still assigns it a `threadId` (equal to its own message id in this case) — every Gmail document has a `thread_id`, never null in practice. No "is this part of a thread" special-casing needed. |
| Incremental sync (`history.list`) | Identical handling to initial sync — `_fetch_document` calls `messages.get(format=full)` in both paths, so `threadId` is available the same way regardless of which sync path added the message. |
| Cross-user/cross-account `thread_id` collision | Not a data-isolation concern — every query already filters by `user_id`/`connection_id` independently of `thread_id`; a coincidental string match (astronomically unlikely given Gmail's id format) wouldn't leak data across tenants even if it occurred. |

## 8. Open follow-ups (not decided here)

- **Result grouping/collapsing by `thread_id`** (Elasticsearch `collapse` query) — no current requirement; revisit if search results being cluttered by same-thread duplicates becomes an actual observed problem.
- **Quoted reply text inflating `body_text`** — now specced separately in `specs/gmail-quote-stripping.md`; not solved by capturing `thread_id` alone.
- **`In-Reply-To`/`References` for exact reply-tree reconstruction** — deferred per §3; revisit only if a feature needs reply *order*, not just conversation *membership*.
- **Slack `thread_ts` / Notion page hierarchy** — deferred, explicitly out of scope (§1); would need their own spec.

## 9. Verification

**Automated**: a `gmail_mime.py` test asserting `thread_id` is extracted correctly from a fixture message JSON (mirroring the existing MIME-parsing tests). A `GmailConnector` test (same `httpx.MockTransport` pattern as `test_gmail_connector.py`) asserting a fetched `Document` carries the right `thread_id`.

**Manual end-to-end**: connect Gmail, sync, query Postgres directly (`SELECT external_id, thread_id FROM documents`) and confirm a known original+reply pair share the same `thread_id` value, while an unrelated email has a different one.
