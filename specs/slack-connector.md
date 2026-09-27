# Spec: Connect Slack as a Data Source

Status: **Draft — not yet agreed, not implemented.**
Owner: findr
Related: `CLAUDE.md` (project goal, hexagonal architecture, spec-driven development), `specs/gmail-connector.md` (established the `SourceConnector`/`OAuthProvider` ports and the auth/sync patterns this spec reuses without change).

## 1. Purpose & scope

Let a Findr user connect their own Slack account (a specific workspace + their identity in it) so messages they can see become searchable alongside their other connected sources.

This is the second connector built on the hexagonal foundation `gmail-connector.md` established. The goal is to prove the `SourceConnector`/`OAuthProvider` ports genuinely need no rework for a second, structurally different source (channel/thread messages instead of email) — not to redesign them.

### In scope
- Connecting a Slack account via Slack's OAuth v2 **user token** flow (not a bot/workspace install) — each Findr user authorizes on their own behalf, exactly like Gmail.
- Background sync of messages from conversations the connecting user is a member of: public channels, private channels, DMs, and group DMs.
- Keyword search over synced Slack messages, scoped to the requesting user, merged into the same `documents`/`documents_fts` store and `SearchDocuments` use case Gmail already uses.
- Token refresh (Slack token rotation), revocation, rate-limit backoff, dedup, and best-effort deletion handling (see §8).

### Out of scope (this feature)
- Bot/workspace-wide installation — a single Slack app install would see everything the *app* has scopes for, not just what the *connecting user* can see, breaking per-user isolation. Only the user-token flow is used.
- Files/attachments shared in Slack — not fetched, not indexed (mirrors Gmail attachments being out of scope).
- Slack Enterprise Grid multi-workspace / org-wide search API — out of scope; one connection = one workspace + one user.
- Real-time delivery via Slack's Events API/Socket Mode — MVP uses the same scheduler-tick polling model as Gmail, not push. (Slack does support push; deferred, see §11.)
- Reliable deletion detection (see §8 and §10) — Slack's history APIs don't expose a changelog the way Gmail's `history.list` does; deletion handling here is best-effort, not guaranteed.
- Search relevance/ranking tuning beyond what SQLite FTS5 already gives Gmail.

## 2. Domain model changes

No new fields. Reuses `domain/entities.py` and `domain/value_objects.py` from `gmail-connector.md` as-is, with one addition:

```python
class SourceType(str, Enum):
    GMAIL = "gmail"
    SLACK = "slack"   # new
```

**Field mapping into the existing `Document` entity** (deliberately not adding Slack-specific fields — see the field-fit discussion below):

| `Document` field | Slack meaning |
|---|---|
| `external_id` | `"{channel_id}:{ts}"` (Slack's per-channel message timestamp is its unique id within a channel; not globally unique alone) |
| `subject` | `None` — Slack messages have no subject |
| `sender` | Sending user's display name, resolved via `users.info` (cached — see §7) |
| `recipients` | The conversation's human-readable label: `"#channel-name"` for public/private channels, `"DM with <name>"` for 1:1, `"Group DM: <name1>, <name2>, ..."` for MPIM — repurposing this field for searchable channel context, same spirit as it holding an email's To-line |
| `body_text` | The message's plain text (`text` field; Slack's `mrkdwn` markup is left as-is rather than stripped, same level of effort as Gmail's HTML-to-text fallback) |
| `sent_at` | Message `ts` converted from Slack's float epoch-seconds string to `datetime` |

**Open question, not decided here:** whether `Document` should eventually gain a real `channel`/`context` field instead of overloading `recipients`. Deferred — see §11.

## 3. Ports

No new ports and no changes to existing port signatures. `SlackOAuthProvider` implements `ports.oauth_provider.OAuthProvider` in full (including `refresh`/`revoke`, discussed in §6); `SlackConnector` implements `ports.source_connector.SourceConnector` in full, identically shaped to `GmailConnector`.

## 4. Application use cases

Mirrors `sources/connect_gmail.py` exactly, new files:

- `sources/connect_slack.py` — `BeginSlackConnect(oauth_provider, oauth_states).execute(user_id) -> authorize_url`; `CompleteSlackConnect(oauth_provider, oauth_states, connection_repo, credential_store).execute(code, state) -> SourceConnection`. Same PKCE-optional note in §6.
- No changes to `sync/sync_source.py`, `sources/list_connections.py`, `sources/disconnect_source.py`, or `search/search_documents.py` — all already generic over `SourceConnector`/`SourceType`.

## 5. Auth design

Same session/cookie/`oauth_states` design as `gmail-connector.md` §5, unchanged. One addition: because a Slack connection is scoped to *(workspace, user)*, not just an email address, `SourceConnectionRepository.get_by_account` is keyed on `external_account = "{team_id}:{user_id}"` (not a display string) so re-running connect against the same workspace+user reliably finds the same row.

The human-readable `"{workspace_name} ({user_display_name})"` label is a separate `SourceConnection.display_name` field (added after this spec's initial implementation, once the raw `team_id:user_id` dedup key proved unreadable in `GET /sources`). `OAuthProvider.get_display_name(access_token)` resolves it (via `openid.connect.userInfo` + `team.info`, falling back to just the user name if `team.info` fails); `CompleteSlackConnect` sets it on create and refreshes it via `update_display_name()` on reconnect, in case the workspace or user was renamed. `GET /sources` falls back to `external_account` when `display_name` is unset (e.g. rows created before this field existed).

## 6. Slack OAuth flow

**Library**: raw OAuth 2.0 over `httpx`, consistent with `GmailOAuthProvider` (no `slack_sdk` — same reasoning as Gmail's §6: keep it thin, avoid a sync-first SDK).

**Flow**: Slack OAuth v2 (`/oauth/v2/authorize` → `oauth.v2.access`). Slack's OAuth v2 does **not** support PKCE; `BeginSlackConnect`/`CompleteSlackConnect` still create an `oauth_states` row for CSRF protection via `state`, but there is no `code_verifier` — `OAuthStateRepository.create()` is called with an empty/unused verifier string for this connector (no port change; the field is simply unused, exactly like Notion in the sibling spec).

**Scopes** — requested as **user token scopes** (`user_scope` param, not `scope`, which would request bot scopes instead):
- `channels:history`, `channels:read` — public channels
- `groups:history`, `groups:read` — private channels
- `im:history`, `im:read` — DMs
- `mpim:history`, `mpim:read` — group DMs
- `users:read`, `users:read.email` — resolve sender names; email is used as a fallback account identifier if OIDC scopes aren't granted
- `openid`, `profile`, `email` (Sign in with Slack / OIDC) — used to fetch a stable `user_id`/`team_id`/email for `get_account_email`/dedup key, so identity resolution doesn't depend on `users:read.email` being approved

**Token exchange**: `POST https://slack.com/api/oauth.v2.access` with `client_id`, `client_secret`, `code`, `redirect_uri`. The **user token** (`authed_user.access_token`) is what's stored and used for all subsequent calls — not the top-level `access_token`, which is the bot token and is discarded (unused in this feature).

**Token rotation / refresh**: Slack's newer apps support **token rotation**: the user access token expires (~12h) and a `refresh_token` is issued alongside it. `SlackOAuthProvider.refresh()` calls `oauth.v2.access` with `grant_type=refresh_token`. This must be enabled in the Slack app config (see §9) — without it, tokens don't expire and `refresh()` is never invoked (`SyncSource`'s expiry check simply never trips, which is safe either way since Slack tokens without rotation don't expire).

**Revoke**: `POST https://slack.com/api/auth.revoke` with the user token.

**Identity for `get_account_email`**: call `openid.connect.userInfo` (if OIDC scopes were granted) for `{sub, email, https://slack.com/team_id}`; the returned value is `"{team_id}:{user_id}"` (the dedup key from §5), with the human-readable label built from `openid.connect.userInfo`'s `name`/`email` plus a `team.info` call for the workspace name.

## 7. Background sync

- **No single cross-conversation cursor exists in Slack's API** (unlike Gmail's `historyId`). `sync_cursor` stores a JSON object mapping `channel_id -> last_synced_ts`, e.g. `{"C0123": "1700000000.000100", "D0456": "1700000100.000200"}`. This is still one opaque string as far as `SourceConnectionRepository`/`SourceConnection.sync_cursor: str | None` are concerned — no schema or port change, same trick used for any connector without a single monotonic cursor.
- **Initial sync** (`cursor is None`): `conversations.list` (types: `public_channel,private_channel,mpim,im`) to enumerate conversations the user is a member of, paginated; for each, `conversations.history` from the start, capped per-conversation (mirrors Gmail's `MAX_MESSAGES_PER_INITIAL_SYNC`, applied per-channel here) to bound a single sync tick.
- **Incremental sync**: for each conversation in the stored cursor map (plus any new conversation from a fresh `conversations.list` — Slack gives no cross-conversation "what's new" signal, so re-listing conversations every tick is required), `conversations.history` with `oldest={last_synced_ts}`.
- **User name cache**: `users.info` results cached in-memory per `SlackConnector` instance for the duration of one sync call (a workspace's user list is small and stable enough not to warrant a persistent cache in MVP).
- **Rate limiting**: same `tenacity`-based exponential backoff on 429, honoring `Retry-After`, as Gmail. Slack's per-method tier limits are stricter than Gmail's, so backoff matters more here.
- **Deletions**: see §10 — best-effort only, not attempted as a first pass. `ChangeBatch.deleted_external_ids` is `[]` for this connector initially.

## 8. Prerequisite: Slack app setup (manual, done once by the developer)

1. Create a Slack app at api.slack.com/apps ("From scratch").
2. **OAuth & Permissions**: add the **User Token Scopes** listed in §6 (not Bot Token Scopes). Add the OAuth Redirect URL: `http://localhost:8000/sources/slack/callback`.
3. Enable **Token Rotation** (Settings → OAuth & Permissions, or Enhanced Token Security) so `refresh()` is exercised.
4. Enable **Sign in with Slack** (OpenID Connect) if using `openid`/`profile`/`email` scopes for identity.
5. Install the app to a development workspace (or leave it uninstalled and let real users install-and-authorize via the connect flow, same as Gmail's Testing-mode pattern).
6. Put `SLACK_CLIENT_ID`/`SLACK_CLIENT_SECRET` into `.env`.

## 9. Data model

No SQL schema changes — reuses `source_connections` and `documents`/`documents_fts` from `gmail-connector.md` §7 verbatim. `source_type='slack'` is just another row value.

## 10. Edge cases

| Edge case | Handling |
|---|---|
| Token expiry (rotation enabled) | Same `NEEDS_REAUTH` path as Gmail, via `SourceAuthError` on a failed refresh |
| Token rotation disabled on the Slack app | Tokens don't expire; `refresh()` is simply never called — safe no-op path, not an error |
| Revoked access (user removes the app in Slack) | API calls return `invalid_auth`/`token_revoked` → mapped to `SourceAuthError` → `NEEDS_REAUTH` |
| Rate limiting | Exponential backoff honoring `Retry-After`, isolated per connection, same as Gmail |
| User joins a new channel after initial sync | Picked up on the next tick's `conversations.list` re-enumeration (see §7) |
| Message edited | Re-fetched and upserted on next sync of that conversation (dedup key `channel_id:ts` — Slack keeps the original `ts` on edit, so this naturally updates in place) |
| **Message deleted** | **Not detected in this pass.** `conversations.history` simply omits it; without a diff against the previous fetch, a deletion looks identical to "no new messages." Left in the local index until a future pass adds either (a) a periodic full-range re-fetch-and-diff per conversation, or (b) Slack's Events API (`message_deleted`) via a webhook — both deferred, see §11. |
| User leaves/is removed from a channel | Next `conversations.list` no longer returns it; already-synced messages remain searchable (same "keep indefinitely" stance as Gmail disconnect, §11) |
| Huge conversation history on first connect | Same per-conversation cap approach as Gmail's `MAX_MESSAGES_PER_INITIAL_SYNC`, applied per channel |
| DM/group DM with a deactivated user | `users.info` may 404/return a deactivated user; fall back to the raw user id as `sender` rather than failing the whole sync |

## 11. Open follow-ups (not decided here)

- Real deletion detection (Events API/webhook, or periodic diff) — flagged above, not attempted in v1.
- Whether `Document` needs a real `channel`/`context` field instead of overloading `recipients` — works for MVP, revisit if it causes UI/search-quality problems.
- Whether thread replies need special handling (currently: a threaded reply is just another message with its own `ts`, indexed independently, no thread-grouping in search results).
- Slack Enterprise Grid / org-wide accounts — explicitly out of scope, not designed against.
- Same disconnect-retention question as Gmail (§11 there): keep synced messages after disconnect, or delete them — unresolved for both connectors, should be decided once, not per-connector.

## 12. Verification

**Automated** (`pytest`, in-memory SQLite, no real Slack app needed): same shape as Gmail's — a `FakeSlackConnector`/fake conversations fixture exercising dedup-on-resync via `channel_id:ts`, and a message-normalization test against a fixture `conversations.history` JSON payload.

**Manual end-to-end**: connect Slack via browser OAuth (user-token flow) → wait for a sync tick → search returns messages from channels/DMs that user is in → a second, unconnected user's search returns nothing from the first user's Slack → revoking the app in Slack surfaces `NEEDS_REAUTH` without crashing the scheduler.
