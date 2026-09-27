# Spec: Connect Notion as a Data Source

Status: **Draft — not yet agreed, not implemented.**
Owner: findr
Related: `CLAUDE.md` (project goal, hexagonal architecture, spec-driven development), `specs/gmail-connector.md` (established the `SourceConnector`/`OAuthProvider` ports and the auth/sync patterns this spec reuses without change), `specs/slack-connector.md` (sibling connector, drafted alongside this one).

## 1. Purpose & scope

Let a Findr user connect their Notion workspace so pages they've shared with the Findr integration become searchable alongside their other connected sources.

Third connector on the hexagonal foundation. Notion is the most structurally different of the three so far: content is block-based (not a flat body of text), access is opt-in per page rather than "everything the OAuth scope allows," and Notion's OAuth tokens don't expire — each of these is called out below because it's where this connector's behavior necessarily diverges from Gmail/Slack, not because the ports need to change to accommodate it.

### In scope
- Connecting a Notion workspace via Notion's OAuth 2.0 flow, scoped to one Findr user.
- Background sync of **pages the user has explicitly shared with the Findr integration** (not the whole workspace — see §6's note on Notion's sharing model).
- Extracting searchable plain text from a page's title and top-level block content.
- Keyword search over synced Notion pages, scoped to the requesting user, merged into the existing `documents`/`documents_fts` store.
- Rate-limit backoff, dedup, edit-triggered re-sync.

### Out of scope (this feature)
- Notion databases as structured data (rows/properties/filters/views) — a database's pages are synced like any other page (title + block content), but the database schema itself (property types, views) is not modeled or searchable as structured data.
- Full recursive sync of deeply nested child blocks/sub-pages beyond a bounded depth (see §7) — avoids unbounded fetch cost on large page trees.
- Comments, page history/versions.
- Files/images embedded in pages — not fetched, not indexed (same stance as Gmail attachments, Slack files).
- Notion's newer Webhooks feature for push-based updates — MVP polls, same as Gmail/Slack (§11 defers this).
- Deletion detection beyond what `/v1/search` naturally reflects (see §10) — if a page is deleted or unshared from the integration, it simply stops appearing in `/v1/search` results; this is treated as equivalent to deletion and removed from the index, which is more reliable than Slack's deletion story but still not push-driven.

## 2. Domain model changes

```python
class SourceType(str, Enum):
    GMAIL = "gmail"
    SLACK = "slack"
    NOTION = "notion"   # new
```

**Field mapping into the existing `Document` entity:**

| `Document` field | Notion meaning |
|---|---|
| `external_id` | Notion page id (UUID, stable across edits) |
| `subject` | Page title, extracted from the page's `title`-type property (property key varies — for a database page it's whichever property has `type: "title"`; for a plain page it's the `title` property directly) |
| `sender` | `last_edited_by` user's name, resolved via `users.retrieve` (cached per sync call, same pattern as Slack's `users.info` cache) |
| `recipients` | Parent context: the parent page/database title if resolvable, else the workspace name — repurposed for searchable context, same spirit as Slack's channel-name reuse |
| `body_text` | Plain text concatenated from the page's block children (paragraphs, headings, list items, to-dos, quotes, callouts), see §7 for extraction rules and depth bound |
| `sent_at` | `last_edited_time` (ISO 8601, parsed directly — Notion's timestamps are already RFC3339, no epoch conversion needed unlike Slack) |

**Open question, not decided here:** whether database *rows* should eventually be modeled distinctly from plain pages (e.g. surfacing key properties in search results). Deferred — see §11.

## 3. Ports

No new ports and no changes to existing port signatures. `NotionOAuthProvider` implements `ports.oauth_provider.OAuthProvider` in full — including `refresh()` and `revoke()`, both of which are near-no-ops for Notion (see §6) rather than unsupported; the Protocol is satisfied, just with different runtime behavior per connector, exactly as `SourceConnector` already tolerates Gmail vs. Slack having entirely different sync strategies behind the same `fetch_changes` signature.

## 4. Application use cases

Mirrors `sources/connect_gmail.py`, new file:

- `sources/connect_notion.py` — `BeginNotionConnect(oauth_provider, oauth_states).execute(user_id) -> authorize_url`; `CompleteNotionConnect(oauth_provider, oauth_states, connection_repo, credential_store).execute(code, state) -> SourceConnection`.
- No changes to `sync/sync_source.py`, `sources/list_connections.py`, `sources/disconnect_source.py`, or `search/search_documents.py`.

## 5. Auth design

Same session/cookie/`oauth_states` design as `gmail-connector.md` §5. A Notion connection is scoped to *(workspace, authorizing user)*; `external_account` stores `workspace_id` as the dedup key for `get_by_account`.

The human-readable `"{workspace_name}"` (plus the owner's email, when available — see §6) is a separate `SourceConnection.display_name` field, the same mechanism added for Slack (see `slack-connector.md` §5) once the raw `workspace_id` UUID proved unreadable in `GET /sources`. `OAuthProvider.get_display_name(access_token)` resolves it; `CompleteNotionConnect` sets it on create and refreshes it via `update_display_name()` on reconnect.

## 6. Notion OAuth flow

**Library**: raw OAuth 2.0 over `httpx`, consistent with the other two connectors.

**Flow**: Notion's OAuth 2.0 Authorization Code flow (`/v1/oauth/authorize` → `/v1/oauth/token`). **No PKCE** — Notion's OAuth doesn't support it; the `oauth_states` row is still created for CSRF protection via `state`, with the `code_verifier` field unused (same situation as Slack, §6 there).

**Notion's sharing model — the important divergence**: Notion has no "scope" that grants access to an entire workspace's content. After OAuth, the integration only sees pages the user has explicitly connected to it via each page's "•••" → "Connections" menu (or a parent page they've connected, whose children inherit access). This means:
- The connect flow's authorize screen already lets the user pick which pages/databases to share — there's no separate in-app "select folders" step for Findr to build.
- Newly created pages are **not** automatically visible until the user shares them (or they're created under an already-shared parent) — this is a Notion platform behavior, not something Findr's sync can work around. Documented for the user, not solved here.

**Token exchange**: `POST https://api.notion.com/v1/oauth/token` with HTTP Basic Auth (`client_id:client_secret`) and body `{grant_type: "authorization_code", code, redirect_uri}`.

**Tokens don't expire**: Notion's access tokens are long-lived with no documented expiry or refresh endpoint. `Credentials.expires_at` (a required field on the shared `Credentials` entity) is set to a far-future sentinel (`now + 100 years`) at connect time so `SyncSource`'s proactive-refresh check never trips. `NotionOAuthProvider.refresh()` exists to satisfy the `OAuthProvider` Protocol but is never expected to be called; if it is, it raises `SourceAuthError` (surfacing as `NEEDS_REAUTH` rather than crashing the sync loop) rather than silently returning stale/fabricated credentials.

**No public revoke endpoint**: Notion doesn't expose a token-revocation API as of this writing; disconnecting an integration is a manual step the user takes in Notion's workspace settings. `NotionOAuthProvider.revoke()` is a no-op (logs, doesn't call any API) — `DisconnectSource` still deletes the locally stored credentials and marks the connection `DISCONNECTED`, it just can't force Notion's side to forget the integration. This is called out to the user in the connect/disconnect UI copy (deferred UI work, same status as Gmail's UI wiring).

**Identity for `get_account_email`**: the token-exchange response includes `workspace_id`, `workspace_name`, and (if the integration has the "Read user information including email addresses" capability enabled) `owner.user.person.email`. `get_account_email` returns `workspace_id` (the dedup key); the display label combines `workspace_name` with the owner's email when the capability/claim is present, falling back to just the workspace name when it isn't.

## 7. Background sync

- **Cursor**: `sync_cursor` stores the ISO 8601 `last_edited_time` of the most-recently-seen page from the prior sync (a plain string, no JSON encoding needed here — simpler than Slack's per-channel map since Notion's `/v1/search` gives one unified, sortable stream).
- **Initial sync** (`cursor is None`): `POST /v1/search` with `sort: {direction: "descending", timestamp: "last_edited_time"}`, `filter: {property: "object", value: "page"}`, paginated via `start_cursor`/`has_more`, capped per tick (same `MAX_..._PER_INITIAL_SYNC`-style bound as Gmail/Slack).
- **Incremental sync**: same `/v1/search` call, sorted descending by `last_edited_time`; page through results and stop as soon as a page's `last_edited_time <= cursor` is reached (everything after that point was already synced). New `sync_cursor` = the `last_edited_time` of the first (most recent) result.
- **Block/content extraction**: for each page, `GET /v1/blocks/{page_id}/children`, paginated. Recurse into children **up to depth 2** (page → its direct blocks → one level of nested blocks, e.g. a bulleted list's sub-items) — bounded to avoid a single deeply-nested page turning one sync tick into an unbounded job, same motivation as Gmail's per-sync message cap. Text extracted from `rich_text` arrays on `paragraph`, `heading_1/2/3`, `bulleted_list_item`, `numbered_list_item`, `to_do`, `quote`, and `callout` block types; other block types (images, embeds, tables, code — anything without a simple `rich_text` field) are skipped, not fetched further.
- **Rate limiting**: Notion's documented average limit is ~3 requests/second per integration. Same `tenacity`-based exponential backoff on 429 honoring `Retry-After` as the other two connectors.
- **User name cache**: `users.retrieve` results cached per sync call, same pattern as Slack.

## 8. Prerequisite: Notion integration setup (manual, done once by the developer)

1. Create a **Public integration** at notion.so/my-integrations (a Public integration is required for the multi-tenant OAuth flow — an Internal integration only works for the single workspace it's created in, useful for local dev but not for real users connecting their own workspaces).
2. Under **Capabilities**, enable "Read content" (required) and optionally "Read user information including email addresses" (improves the account label in §6, not required for the connector to function).
3. Add the OAuth redirect URI: `http://localhost:8000/sources/notion/callback`.
4. Put `NOTION_CLIENT_ID`/`NOTION_CLIENT_SECRET` into `.env`.
5. Note for local dev/testing: an Internal Integration Token can stand in for the OAuth flow (skip §6 entirely, use the static token directly as the stored credential) while the Public integration awaits any required Notion review — mirrors Gmail's Testing-mode workaround in `gmail-connector.md` §9.

## 9. Data model

No SQL schema changes — reuses `source_connections` and `documents`/`documents_fts` from `gmail-connector.md` §7 verbatim. `source_type='notion'` is just another row value.

## 10. Edge cases

| Edge case | Handling |
|---|---|
| Token doesn't expire | `expires_at` set to a far-future sentinel at connect time; `refresh()` is never expected to be called (§6) |
| Revoked access (user removes integration in Notion settings) | Next `/v1/search` call returns 401 → mapped to `SourceAuthError` → `NEEDS_REAUTH`, same path as Gmail/Slack even though there's no revoke-initiated push signal |
| Page unshared from the integration | Stops appearing in `/v1/search`; **treated as deleted** — removed from the local index on the next sync that no longer sees it in the full incremental scan (requires comparing the previously-synced id set to the current one; see implementation note below) |
| Page deleted in Notion | Same handling as "unshared" — Notion's API doesn't distinguish the two from the integration's point of view |
| Newly created page not yet shared | Invisible to sync until the user shares it (or a shared parent) — platform limitation, documented for the user, not a bug |
| Rate limiting | Exponential backoff honoring `Retry-After`, isolated per connection |
| Deeply nested page content | Bounded to depth 2 (§7); content beyond that depth is simply not indexed, not an error |
| Database page with no title property visible | Falls back to `"Untitled"` rather than failing the sync for that page |
| Huge workspace shared all at once | Same per-tick cap as Gmail/Slack initial sync |

**Implementation note on deletion**: unlike Gmail's `history.list` (which reports deletions explicitly) or Slack (where deletion detection is deferred entirely, per `slack-connector.md` §10), Notion's `/v1/search` is a full live view of currently-accessible pages. Detecting a removal requires diffing the full set of `external_id`s returned against what's already stored for that connection and deleting the difference — a different shape of work than the "process a stream of changes" model Gmail/Slack use. This diff runs once per full incremental sync pass (not per page), so its cost is one extra set-difference against already-fetched ids, not additional API calls.

## 11. Open follow-ups (not decided here)

- Whether database rows deserve distinct modeling (property values as structured, filterable search fields) instead of being treated as plain pages — deferred, revisit once real usage shows plain-page treatment is insufficient.
- Push-based sync via Notion's Webhooks feature, once evaluated against the polling model already established for Gmail/Slack.
- UI copy explaining Notion's share-per-page model and the no-revoke-API limitation to end users — deferred alongside the general UI wiring noted in `gmail-connector.md` §11.
- Same disconnect-retention question raised in the Gmail and Slack specs — should be decided once, across all three connectors, not per-connector.

## 12. Verification

**Automated** (`pytest`, in-memory SQLite, no real Notion integration needed): a `FakeNotionConnector`/fake `/v1/search` + `/v1/blocks` fixture exercising dedup-on-resync via page id, the depth-2 block-extraction bound, and the unshare/delete-diff behavior from §10.

**Manual end-to-end**: connect Notion via browser OAuth, share a couple of pages with the integration when prompted → wait for a sync tick → search returns those pages' titles/content → an unshared page doesn't appear → a second, unconnected user's search returns nothing from the first user's Notion → unsharing a previously-synced page removes it from search on the next tick.
