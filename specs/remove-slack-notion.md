# Spec: Remove the Slack and Notion connectors

Status: **Implemented** (branch `chore/remove-slack-notion`). Written alongside the change rather than before it: this removes features rather than adding one.
Owner: findr
Related: `CLAUDE.md` (project goal: Slack and Notion are now out of scope), and the deleted `specs/slack-connector.md` / `specs/notion-connector.md` (still in git history). Other specs that mention Slack/Notion are annotated rather than rewritten (§6).

## 1. Decision

Findr won't support Slack or Notion. Both connectors were built and working; they are removed rather than left unused, so the code, settings, routes and UI only describe sources the product actually has.

**Supported sources after this change:** Gmail (OAuth, synced by the scheduler) and uploaded files (pushed by the user). WhatsApp and Google Drive remain *planned* (`CLAUDE.md`). Nothing here forecloses them, since the `SourceConnector` and `OAuthProvider` ports are unchanged.

## 2. What was removed

| Area | Removed |
|---|---|
| Adapters | `adapters/outbound/slack/` (connector, OAuth provider, message parsing) and `adapters/outbound/notion/` (connector, OAuth provider, page parsing) |
| Use cases | `application/sources/connect_slack.py`, `connect_notion.py` |
| Domain | `SourceType.SLACK`, `SourceType.NOTION` |
| Wiring | Slack/Notion branches in `connector_factory.oauth_provider_for` / `connector_for` |
| HTTP | `GET /sources/slack/connect`, `/sources/slack/callback`, `/sources/notion/connect`, `/sources/notion/callback` (now `404`) |
| Search | Slack and Notion deep links in `search_router._source_url` |
| Settings | `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_REDIRECT_URI`, `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET`, `NOTION_REDIRECT_URI` (from `config.py` and `.env.example`). Leftover values in a `.env` are harmless: `Settings` ignores unknown variables. |
| Frontend | Slack/Notion source styles, their entries in the "Connect source" menu (Gmail only now), and their filter checkboxes |
| Tests | Their 6 dedicated test files. Shared tests that used Slack as a sample now use Gmail. |
| Specs | `specs/slack-connector.md`, `specs/notion-connector.md` |

## 3. What was deliberately kept

These were introduced for Slack/Notion but are still in use:

- **`SourceConnection.display_name`.** Added so Slack/Notion connections had a readable label separate from their dedup key. The uploads connection relies on it now: it's labelled "Uploaded files" and has no `external_account` at all (`specs/file-upload.md` §2.1).
- **`SearchHit.external_account`** and its denormalized copy in the Elasticsearch documents. Slack's deep links were the only reader. It's kept because it's cheap, generic, and the obvious place for a future source whose links need account-level information. Removing it would mean a mapping change plus a reindex, for no benefit.
- **The `SourceConnector` / `OAuthProvider` ports** and `connector_factory.py`, the extension points for WhatsApp/Google Drive. Their docstrings no longer use Slack/Notion as examples.

## 4. Data

Removing the enum values means any stored row with `source_type` `slack` or `notion` can no longer be read. `SourceType(row.source_type)` would raise, breaking `GET /sources` and the sync scheduler's tick.

- **Dev environment, checked before removal:** `source_connections` held only `gmail` and `file` rows, and the Elasticsearch index had 0 `slack`/`notion` documents. Nothing to clean up.
- **Any other environment** must delete Slack/Notion data **before** deploying this change:

```sql
-- documents (and any chunks) of Slack/Notion connections, then the connections
DELETE FROM document_chunks WHERE document_id IN (
  SELECT d.id FROM documents d JOIN source_connections c ON c.id = d.connection_id
  WHERE c.source_type IN ('slack', 'notion'));
DELETE FROM documents WHERE connection_id IN (
  SELECT id FROM source_connections WHERE source_type IN ('slack', 'notion'));
DELETE FROM source_connections WHERE source_type IN ('slack', 'notion');
```

```sh
curl -X POST "localhost:9200/findr_documents/_delete_by_query" -H 'content-type: application/json' \
  -d '{"query": {"terms": {"source_type": ["slack", "notion"]}}}'
```

The stored OAuth tokens are deleted with their `source_connections` rows; they're columns on that table. The grants themselves aren't revoked on Slack's or Notion's side, since the revoke code is gone. If that matters, revoke the app's access from the Slack workspace or Notion settings first. No schema change is needed.

## 5. Behaviour after the change

- `GET /sources` lists only Gmail and "Uploaded files" connections.
- `/sources/slack/*` and `/sources/notion/*` return `404`.
- The "Connect source" menu offers only Gmail, and it hides once Gmail is connected.
- Search, uploads and Gmail sync are unchanged.

## 6. Other specs

Specs describe how the system came to be, so mentions of Slack/Notion were handled two ways:
- **Statements about current behaviour** were corrected: `file-upload.md`, `semantic-search.md` §12 and `upload-chunking.md`.
- **Historical statements** keep their text, with a short note pointing here: `gmail-connector.md`, `gmail-thread-id.md`, `gmail-quote-stripping.md`, `postgres-migration.md`, `elasticsearch-search.md`.

## 7. Verification

- **Automated:** the full suite passes (133 passed; 4 skipped tests need tesseract/poppler, as before). The deleted tests covered only removed code. Test changes:
  - shared tests use Gmail as the sample source;
  - new test: uploaded files have no deep link (`_source_url` returns `None`).
- **Manual (running stack):** `GET /healthz`, `GET /sources` and `GET /search` all return `200`; `GET /sources/slack/connect` and `GET /sources/notion/connect` return `404`; the worker restarts cleanly.
