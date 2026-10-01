# Spec: Workspaces (one per client)

Status: **Implemented** (branch `feature/workspaces`). What changed or was found during implementation is recorded in §12.
Owner: findr
Related: `CLAUDE.md` (multi-tenant isolation principle, which this extends from users down to workspaces), `specs/gmail-connector.md` (OAuth connect flow), `specs/file-upload.md` and `specs/upload-chunking.md` (uploads), `specs/elasticsearch-search.md` and `specs/semantic-search.md` (search).

## 1. Purpose & scope

One Findr user serves several clients and needs each client's data kept apart. A **workspace** is a named container inside a user's account, one per client. Every Gmail connection and every uploaded file belongs to exactly one workspace, and **search only ever looks inside the workspace you're in**. A document uploaded to client B's workspace can never appear in a search in client A's workspace.

Decisions agreed before this spec:
| Question | Decision |
|---|---|
| Who uses a workspace? | **Just the account owner.** Workspaces are containers inside one user's account: no members, invitations or roles. |
| Users | **One user for now: the owner, who is also the admin.** No roles or admin-only features are needed. Data stays scoped per user anyway (`CLAUDE.md`'s multi-tenant principle), so adding users later needs no rework. |
| How do Gmail accounts map to clients? | **One account per client.** A Gmail account is connected to exactly one workspace, and all its mail belongs to that client. |
| What does search cover? | **The current workspace only.** No cross-workspace search. |
| Existing data | **Start fresh.** Existing connections, documents and uploads are deleted; Gmail is reconnected and files re-uploaded into the right workspaces (§8). |
| Creating workspaces | Through the API and the UI (create, rename, delete). |

### In scope
- A `Workspace` entity, stored in Postgres, with create, list, rename and delete.
- Every source connection and every upload belongs to one workspace. Documents inherit it from their connection.
- Search, the source list, the upload list and the Gmail connect flow, all scoped to one workspace.
- Elasticsearch documents carry `workspace_id`, and every search filters on it as well as `user_id`.
- UI: a workspace switcher (with "+ New workspace", rename and delete), and a first-run "create your first workspace" screen.
- The one-off data reset (§8).

### Out of scope
- Sharing workspaces with other people (members, roles, invitations).
- Searching across workspaces.
- One Gmail inbox split across several workspaces (by label or sender).
- Moving a connection or document from one workspace to another. Reconnect or re-upload instead.
- Per-workspace settings (e.g. a different similarity floor per client).
- A general migration tool; the reset in §8 is one-off.

## 2. Isolation model

**Today:** everything is scoped by `user_id`. **After this change:** everything is scoped by `user_id` **and** `workspace_id`.

| Data | How it's scoped |
|---|---|
| `workspaces` | `user_id` (owner) |
| `source_connections` | `workspace_id` (new, required). The owner is still `user_id`, kept for the existing per-user uniqueness rule (§5.2). |
| `documents` | `workspace_id` (new, required, indexed): a copy of its connection's workspace, derived when the row is written (§3.2). Not needed for isolation, but it makes "list a workspace's documents" a simple indexed query later. |
| `document_chunks` | Via its document's connection (isolation is enforced there), **plus** `workspace_id` in each chunk's metadata, so every chunk describes itself (§3.1). |
| `uploaded_files` | `workspace_id` (new, required). An upload has no connection until the worker processes it, so it needs its own. |
| `oauth_states` | `workspace_id` (new, required), carrying the target workspace through Google's redirect (§5.1). |
| Elasticsearch documents | `workspace_id` (new, keyword), copied onto each document at index time like `source_type`. Elasticsearch has no joins. |

**Rules:**
- **The server never trusts a workspace id on its own.** Every workspace-scoped request first loads the workspace by `(workspace_id, user_id)`. If it doesn't exist or belongs to another user, the answer is `404`, the same as for any other resource (don't confirm another user's id exists).
- **Search filters on both `user_id` and `workspace_id`** in both legs, keyword and semantic. `user_id` stays as a second guard.
- **Item endpoints** (`/uploads/{id}`, `/documents/{id}`, `/sources/{id}`) keep checking ownership by user. An item belongs to exactly one workspace, so its id already pins it down; no workspace parameter is needed.

## 3. Domain & ports

```python
@dataclass
class Workspace:
    id: int
    user_id: int
    name: str
    created_at: datetime

class WorkspaceRepository(Protocol):
    def create(self, user_id: int, name: str) -> Workspace: ...
    def get(self, workspace_id: int, user_id: int) -> Workspace | None: ...   # None if missing or not theirs
    def list_for_user(self, user_id: int) -> list[Workspace]: ...             # by name
    def get_by_name(self, user_id: int, name: str) -> Workspace | None: ...  # case-insensitive
    def rename(self, workspace_id: int, name: str) -> None: ...
    def delete(self, workspace_id: int) -> None: ...
```

New domain exceptions: `WorkspaceNotFound` and `DuplicateWorkspaceName`, plus `SourceInOtherWorkspace` (§5.2).

**Changes to existing ports:**

| Port | Change |
|---|---|
| `SourceConnection` / `UploadedFile` (entities) | gain `workspace_id: int` |
| `SourceConnectionRepository` | `create(...)` takes `workspace_id`; new `list_for_workspace(workspace_id)`; new `get_in_workspace(workspace_id, source_type, external_account)` for the per-workspace "Uploaded files" connection. `get_by_account(user_id, …)` stays user-wide, since Gmail uniqueness is per user (§5.2). |
| `UploadedFileRepository` | `create(...)` takes `workspace_id`; `list_for_user` becomes `list_for_workspace(workspace_id)` |
| `OAuthStateRepository` | `create(...)` takes `workspace_id`; `OAuthState` carries it |
| `Document` | gains optional `workspace_id`, read-only in practice (§3.2) |
| `ChunkMetadata` / `DocumentParser` | `ChunkMetadata.workspace_id: int`; `parse(content, mime_type, filename, document_version, workspace_id)` (§3.1) |
| `SearchIndex` | `search(user_id, workspace_id, query)`; `index_documents(documents, source_type, external_account, workspace_id)`, so the workspace travels with the other connection-level facts, as `source_type` already does; new `delete_workspace(workspace_id)` |

### 3.1 Workspace on every chunk

`ChunkMetadata` (`specs/upload-chunking.md` §4.1/§6.3) gains `workspace_id: int`, alongside `document_version` and `content_sha256`:
- **Why**, given that isolation doesn't depend on it: a chunk can only be reached through its document, and search filters on the document's workspace. The field makes each chunk self-describing (a chunk row or search hit names its client directly, which helps debugging). It also keeps chunks filterable on their own if they're ever stored outside their parent document, e.g. a separate vector store or chunk index.
- **The id, not the name:** names can be renamed, so a stored name would go stale.
- **Can't drift:** documents never move between workspaces (§1), so a chunk's `workspace_id` always equals its connection's.
- **Where it's set:** `ProcessUpload` passes the upload's `workspace_id` to `DocumentParser.parse(...)`, which stamps it on every chunk, as it already does with the version and filename.
- **Storage:**
  - Postgres: a real, indexed column on `document_chunks` (like `document_version` and `content_sha256`), since a future per-chunk filter would query it;
  - Elasticsearch: `chunks.metadata.workspace_id` as a `keyword`.
- **Search still filters on the parent document's `workspace_id`.** The nested kNN filter applies to top-level fields, so the chunk-level copy adds no extra check today.

### 3.2 Workspace on every document row

`documents.workspace_id` is a real column (`NOT NULL`, foreign key to `workspaces.id`, indexed). There's no feature using it yet; it's for the expected "list all documents in a workspace" view, which can then query `documents` directly instead of joining through `source_connections`.
- **Callers never supply it.** `DocumentRepositoryPostgres.upsert_many` fills it from the document's connection inside the same `INSERT` (`SELECT workspace_id FROM source_connections WHERE id = :connection_id`), so it can't disagree with the connection. `SyncSource`, the Gmail connector and `ProcessUpload` don't change for this.
- **Read side:** `Document` gains an optional `workspace_id: int | None = None`, filled in when a document is read from Postgres (`get`). Documents built by connectors leave it unset; on write it's ignored in favour of the connection's.
- **Can't drift:** documents never move between workspaces (§1), so the copy stays correct for the document's lifetime.

`SyncSource` passes `connection.workspace_id` through to `index_documents`. The Gmail connector itself doesn't change: it never needs to know which workspace it's syncing into.

## 4. Workspace management

| Operation | Rules |
|---|---|
| **Create** | Name required: trimmed, 1–100 characters. Names are unique per user, ignoring case: "Client A" and "client a" clash, giving `409`. |
| **List** | The user's workspaces, sorted by name. |
| **Rename** | Same name rules. Renaming to the same name is a no-op. |
| **Delete** | Deletes **everything in the workspace** (below), as agreed. The UI asks for confirmation first. |

**What deleting a workspace removes** (`DeleteWorkspace` use case):
1. For each Gmail connection: revoke the OAuth grant with Google (best effort; a failed revoke is logged and doesn't block the delete), then delete its stored credentials.
2. Delete its uploads: `uploaded_files` rows in any status, and the stored originals on disk.
3. Delete its documents and their chunks.
4. Delete its connections.
5. Delete its Elasticsearch documents (`SearchIndex.delete_workspace`).
6. Delete the workspace row.

Postgres steps run in one transaction. File and Elasticsearch deletion happen after the commit, so a failure there can leave orphaned files or index entries, but never a half-deleted workspace in Postgres. Orphaned index entries can't surface in search: their workspace id no longer exists, and no request can be made with a workspace id the user doesn't own. An upload being processed when its workspace is deleted is already handled: the worker finds its row gone and discards its results (`specs/upload-chunking.md` §5.3).

Deleting a user's last workspace is allowed. The UI then shows the first-run screen (§7).

## 5. Sources and uploads per workspace

### 5.1 Connecting Gmail into a workspace
- `GET /workspaces/{workspace_id}/sources/gmail/connect` checks the workspace belongs to the user, then stores `workspace_id` in the OAuth state along with `user_id` and the PKCE verifier.
- The callback, `GET /sources/gmail/callback`, is unchanged in shape: it still ignores the browser session, and now takes **both** the user and the workspace from the stored state (`specs/gmail-connector.md` §5).
- The new connection is created in that workspace.

### 5.2 One Gmail account, one workspace
The existing unique constraint on `(user_id, source_type, external_account)` already stops the same Gmail account from being connected twice by one user. With workspaces, that means **an account can be in only one of your workspaces**.
- **Reconnecting** an account into the **same** workspace works as today: the tokens are refreshed and the connection is reactivated.
- **Connecting** an account that's already in a **different** workspace is **refused** (`SourceInOtherWorkspace`). The callback redirects to `/?connect_error=…` with a message naming the other workspace ("This Gmail account is already connected in workspace 'Client A'"), and the UI shows it.
- This also applies when the existing connection is disconnected: its documents are still in the other workspace. Moving the account requires deleting it from the other workspace first, and per-connection deletion doesn't exist yet. Deleting the whole workspace frees it (§10).

### 5.3 Uploads
- `POST /workspaces/{workspace_id}/uploads` replaces `POST /sources/upload`. The upload row records `workspace_id`.
- The worker creates or reuses that workspace's own "Uploaded files" connection, so there's one per workspace, not one per user.
- Everything else in `specs/upload-chunking.md` is unchanged.

### 5.4 Background sync
The scheduler still syncs every active Gmail connection regardless of workspace. Each connection knows its workspace, and `SyncSource` indexes its documents with it.

## 6. API

**New:**
| Endpoint | Purpose |
|---|---|
| `GET /workspaces` | List: `[{id, name, created_at}]` |
| `POST /workspaces` `{name}` | Create → `201`; `409` on a duplicate name; `422` on a blank or too-long name |
| `PATCH /workspaces/{id}` `{name}` | Rename → `200`; same errors |
| `DELETE /workspaces/{id}` | Delete with everything in it → `204` |

**Workspace-scoped**, replacing user-wide endpoints:
| Before | After |
|---|---|
| `GET /search?q=` | `GET /workspaces/{id}/search?q=` |
| `GET /sources` | `GET /workspaces/{id}/sources` |
| `GET /sources/gmail/connect` | `GET /workspaces/{id}/sources/gmail/connect` |
| `POST /sources/upload` | `POST /workspaces/{id}/uploads` |
| `GET /uploads` | `GET /workspaces/{id}/uploads` |

The old endpoints are removed rather than kept, because a user-wide search is exactly what workspaces exist to prevent. The frontend is the only client.

**Unchanged** (item ids already pin the workspace): `GET`/`DELETE /uploads/{id}`, `DELETE /documents/{id}`, `POST /sources/{id}/sync`, `DELETE /sources/{id}`, `GET /sources/gmail/callback`. Every `/workspaces/{id}/…` route returns `404` for a workspace the user doesn't own.

## 7. UI

- **Workspace switcher** at the top of the sidebar: the current workspace's name, opening a menu that lists all workspaces, plus "+ New workspace", "Rename" and "Delete". New and Rename ask for a name; Delete asks for confirmation and says everything in the workspace will be removed.
- **The current workspace is remembered** in the browser (`localStorage`), as a convenience only. On load it's checked against `GET /workspaces`, falling back to the first workspace by name. Every API call passes the workspace id explicitly.
- **Switching workspace** clears the search box and results, and reloads the sources and uploads lists, so nothing from the previous client stays on screen.
- **First run / no workspaces:** a "Create your first workspace" screen with a name field replaces search, sources and uploads until one exists.
- **Connect errors:** a `connect_error` in the URL after the Gmail redirect is shown as a message, then removed from the address bar.
- The page title area shows the current workspace's name, so it's always clear which client you're looking at.

## 8. Data reset ("start fresh")

Agreed decision: existing data is not migrated. This is the one-off reset for an existing environment, run once when deploying:

1. **Optional:** disconnect the Gmail account in the UI first, so Google's grant is revoked. Otherwise remove the app's access from your Google account settings afterwards.
2. **Postgres:** drop `document_chunks`, `uploaded_files`, `documents`, `source_connections` and `oauth_states`. The app's `create_all` then recreates them with the new columns, and creates `workspaces`. `users` and `sessions` are untouched, so you stay logged in. (No migration tool exists; dropping is consistent with how `uploaded_files` was reshaped in `specs/upload-chunking.md` §13.)
3. **Elasticsearch:** delete the `findr_documents` index; `ensure_index` recreates it with `workspace_id` in the mapping.
4. **Stored originals:** delete everything under the uploads volume (`/data/uploads`).
5. **Redis:** queued upload jobs refer to deleted uploads. They're skipped harmlessly when processed (`claim` finds no row), so no action is needed.

The same reset applies to the test database, whose tables would otherwise keep the old shape.

## 9. Edge cases

| Edge case | Handling |
|---|---|
| A request for a workspace that's someone else's, or doesn't exist | `404`, identical in both cases |
| Two workspaces with the same name in different case | `409` on create or rename |
| Gmail account already connected in another workspace | Refused, with the redirect message (§5.2) |
| Connect started in workspace A, then A deleted before Google redirects back | The OAuth state references a deleted workspace. The callback treats it as an invalid state (`400`), and nothing is created. |
| Upload in progress when its workspace is deleted | The worker finds the upload gone and discards its results (§4) |
| Gmail sync running while its workspace is deleted | The sync's writes may land after the delete. The delete removes the connection first, so the sync's commit fails its foreign key, the sync records the error, and nothing is indexed under the deleted workspace. Accepted rarity: the scheduler runs every few minutes. |
| Same filename uploaded to two workspaces | Two independent uploads and documents, one in each workspace |
| Last workspace deleted | First-run screen (§7) |
| Browser remembers a workspace that was deleted (e.g. in another tab) | Not in `GET /workspaces` on load, so the UI falls back to the first workspace. A scoped call made meanwhile gets `404`, and the UI reloads the workspace list. |

## 10. Open follow-ups (not decided here)

- Removing a single connection together with its documents. This is needed to move a Gmail account from one workspace to another without deleting the whole workspace.
- Moving uploads between workspaces.
- Sharing a workspace with other users (members, roles).
- Optional cross-workspace search, if ever wanted, as an explicit, clearly-labelled mode.
- Per-workspace settings.

## 11. Testing

- **Repositories (Postgres):**
  - workspace create, list, rename and delete;
  - name uniqueness ignoring case, per user;
  - a user can't get another user's workspace;
  - connections, uploads and OAuth states round-trip `workspace_id`;
  - chunk rows store `workspace_id` as a column, and it round-trips into `ChunkMetadata`;
  - `upsert_many` sets `documents.workspace_id` from the connection even when the `Document` passed in has none or a different value, and `get` returns it.
- **Parser:** every chunk carries the `workspace_id` it was given.
- **Use cases (fakes):**
  - `CreateWorkspace` / `RenameWorkspace` name rules;
  - `DeleteWorkspace` removes connections, credentials (with revoke), documents, chunks, uploads, files and index entries, and continues past a failed revoke;
  - `CompleteGmailConnect` creates the connection in the state's workspace, reconnects within the same workspace, and refuses an account that's in another workspace;
  - `UploadFile` / `ProcessUpload` use the per-workspace "Uploaded files" connection.
- **Elasticsearch adapter:**
  - **the isolation test:** the same text indexed in workspace A and workspace B, and a search in A finds only A's copy, for both keyword and semantic matches;
  - `delete_workspace` removes only that workspace's documents;
  - nested chunks carry `metadata.workspace_id`, mapped as `keyword`.
- **Router (end to end):**
  - every `/workspaces/{id}/…` route returns `404` for another user's workspace;
  - an upload into B never appears in A's search, sources or upload list;
  - the old user-wide endpoints are gone;
  - the Gmail callback refuses an account that's in another workspace;
  - deleting a workspace leaves the other workspaces untouched.
- **Scheduler:** documents synced for a connection are indexed with that connection's workspace.
- **Manual:** create workspaces "Client A" and "Client B"; upload different files to each; confirm each search sees only its own files; connect Gmail into one workspace and confirm the other can't connect the same account; delete a workspace and confirm the other is untouched.

## 12. Implementation notes

- **Bug found by the tests:** the case-insensitive unique name rule was first written as `Index(..., func.lower("name"))`. In SQLAlchemy that indexes the *string literal* `'name'`, not the column, which would have allowed **one workspace per user, whatever its name**. Now `text("lower(name)")`; Postgres reports the index as `(user_id, lower((name)::text))`. A database-level test guards it (`test_workspace_names_are_unique_per_user_ignoring_case_in_the_database`).
- **`oauth_states.workspace_id` uses `ON DELETE CASCADE`.** Deleting a workspace removes its pending connects, so a Google callback arriving afterwards finds no state and is rejected with `400`, as §9 requires, with no extra code in `DeleteWorkspace`.
- **`SourceConnectionRepository.list_for_user` was removed** rather than kept alongside `list_for_workspace`. Nothing needs a user-wide connection list any more, and leaving one around would invite a cross-workspace listing by mistake. `delete(connection_id)` was added for `DeleteWorkspace`.
- **`DocumentRepository.delete_for_workspace`** was added, deleting chunks then documents for `DeleteWorkspace`.
- **Workspace ownership is one dependency:** `get_workspace` in `adapters/inbound/http/deps.py`, wrapping the `GetWorkspace` use case. Every `/workspaces/{workspace_id}/...` route depends on it, so a route can't forget the check.
- **Gmail refusal is a redirect, not an error page:** `/?connect_error=...`. The UI shows it in a dismissable banner, then removes it from the address bar.
- **UI responses are dropped if you've switched workspace:** search, source and upload responses are ignored if the current workspace changed while they were in flight, so a slow response from one client can't render under another's name. A `404` on a scoped call reloads the workspace list (the workspace was deleted in another tab).
- **Data reset (§8), as run on the dev environment:**
  - dropped `document_chunks`, `uploaded_files`, `documents`, `source_connections`, `oauth_states` (and the in-progress `workspaces`), then recreated them on app start;
  - deleted the `findr_documents` index;
  - removed stored files.

  This removed 1 Gmail connection with 305 synced documents, and 1 upload with 4 chunks. The Gmail grant wasn't revoked with Google (step 1 is optional). The test database was reset the same way.

**Verification:**
- **Automated:** the full suite passes (167 passed; 4 OCR tests skipped without tesseract/poppler, as before). New coverage:
  - workspace use cases and router: create/list/rename/delete, name rules, 404 for others' workspaces, delete removes everything and leaves other workspaces intact;
  - the Gmail callback refusing an account that's in another workspace, plus a since-deleted workspace's connect returning 400;
  - search isolation across workspaces, for keyword and semantic matches;
  - `documents.workspace_id` always taken from the connection;
  - chunk `workspace_id` in Postgres and Elasticsearch;
  - each workspace getting its own "Uploaded files" connection.
- **Manual (running stack):**
  - created "Client A" and "Client B" (a duplicate "client a" → `409`), uploaded a different file to each, and the worker processed both;
  - each workspace's searches returned only its own file;
  - Postgres and Elasticsearch carried the right `workspace_id` on documents, connections and chunks;
  - deleting Client B removed its rows, index entries and stored file, and Client A was unaffected.

  The test workspaces were then deleted, leaving no workspaces (the first-run screen). The UI wasn't driven in a browser during implementation; its script passes a syntax check and the page serves the new elements.

