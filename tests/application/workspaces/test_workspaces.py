from dataclasses import replace
from datetime import datetime

import pytest

from findr.application.workspaces.delete_workspace import DeleteWorkspace
from findr.application.workspaces.manage_workspaces import (
    CreateWorkspace,
    GetWorkspace,
    ListWorkspaces,
    RenameWorkspace,
)
from findr.domain.entities import Credentials, SourceConnection, UploadedFile, Workspace
from findr.domain.exceptions import (
    DuplicateWorkspaceName,
    InvalidWorkspaceName,
    WorkspaceNotFound,
)
from findr.domain.value_objects import ConnectionStatus, SourceType, UploadStatus

NOW = datetime(2026, 10, 1)


class FakeWorkspaceRepository:
    def __init__(self) -> None:
        self.rows: dict[int, Workspace] = {}

    def create(self, user_id, name):
        w = Workspace(id=len(self.rows) + 1, user_id=user_id, name=name, created_at=NOW)
        self.rows[w.id] = w
        return w

    def get(self, workspace_id, user_id):
        w = self.rows.get(workspace_id)
        return w if w is not None and w.user_id == user_id else None

    def list_for_user(self, user_id):
        return sorted((w for w in self.rows.values() if w.user_id == user_id), key=lambda w: w.name.lower())

    def get_by_name(self, user_id, name):
        return next(
            (w for w in self.rows.values() if w.user_id == user_id and w.name.lower() == name.lower()),
            None,
        )

    def rename(self, workspace_id, name):
        self.rows[workspace_id] = replace(self.rows[workspace_id], name=name)

    def delete(self, workspace_id):
        self.rows.pop(workspace_id, None)


# ---------- create / rename / get ----------


def test_create_trims_and_lists_by_name():
    repo = FakeWorkspaceRepository()
    CreateWorkspace(repo).execute(1, "  Client B ")
    CreateWorkspace(repo).execute(1, "client a")
    CreateWorkspace(repo).execute(2, "Other user's")

    assert [w.name for w in ListWorkspaces(repo).execute(1)] == ["client a", "Client B"]


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_create_rejects_blank_or_too_long_names(name):
    with pytest.raises(InvalidWorkspaceName):
        CreateWorkspace(FakeWorkspaceRepository()).execute(1, name)


def test_names_are_unique_per_user_ignoring_case():
    repo = FakeWorkspaceRepository()
    CreateWorkspace(repo).execute(1, "Client A")

    with pytest.raises(DuplicateWorkspaceName):
        CreateWorkspace(repo).execute(1, "client a")
    # Another user may use the same name.
    CreateWorkspace(repo).execute(2, "Client A")


def test_rename_rules():
    repo = FakeWorkspaceRepository()
    a = CreateWorkspace(repo).execute(1, "Client A")
    CreateWorkspace(repo).execute(1, "Client B")

    assert RenameWorkspace(repo).execute(a.id, 1, "Client A2").name == "Client A2"
    # Same name (any case) on itself is fine; clashing with another isn't.
    assert RenameWorkspace(repo).execute(a.id, 1, "client a2").name == "client a2"
    with pytest.raises(DuplicateWorkspaceName):
        RenameWorkspace(repo).execute(a.id, 1, "CLIENT B")
    with pytest.raises(WorkspaceNotFound):
        RenameWorkspace(repo).execute(a.id, 2, "Stolen")


def test_get_workspace_is_scoped_to_its_owner():
    repo = FakeWorkspaceRepository()
    a = CreateWorkspace(repo).execute(1, "Client A")

    assert GetWorkspace(repo).execute(a.id, 1) == a
    with pytest.raises(WorkspaceNotFound):
        GetWorkspace(repo).execute(a.id, 2)
    with pytest.raises(WorkspaceNotFound):
        GetWorkspace(repo).execute(999, 1)


# ---------- delete ----------


class FakeConnections:
    def __init__(self, connections):
        self.rows = {c.id: c for c in connections}
        self.deleted: list[int] = []

    def list_for_workspace(self, workspace_id):
        return [c for c in self.rows.values() if c.workspace_id == workspace_id]

    def delete(self, connection_id):
        self.deleted.append(connection_id)
        self.rows.pop(connection_id)


class FakeCredentials:
    def __init__(self, creds):
        self.creds = dict(creds)

    def get(self, connection_id):
        return self.creds.get(connection_id)

    def delete(self, connection_id):
        self.creds.pop(connection_id, None)


class FakeOAuthProvider:
    def __init__(self, fail=False):
        self.revoked: list[Credentials] = []
        self._fail = fail

    def revoke(self, credentials):
        if self._fail:
            raise RuntimeError("google down")
        self.revoked.append(credentials)


class FakeUploads:
    def __init__(self, uploads):
        self.rows = {u.id: u for u in uploads}

    def list_for_workspace(self, workspace_id):
        return [u for u in self.rows.values() if u.workspace_id == workspace_id]

    def delete(self, upload_id):
        self.rows.pop(upload_id)


class FakeDocuments:
    def __init__(self):
        self.deleted_workspaces: list[int] = []

    def delete_for_workspace(self, workspace_id):
        self.deleted_workspaces.append(workspace_id)


class FakeSearchIndex:
    def __init__(self, log):
        self.deleted_workspaces: list[int] = []
        self._log = log

    def delete_workspace(self, workspace_id):
        self._log.append("index")
        self.deleted_workspaces.append(workspace_id)


class FakeStorage:
    def __init__(self, log):
        self.deleted: list[str] = []
        self._log = log

    def delete(self, path):
        self._log.append("file")
        self.deleted.append(path)


class FakeUnitOfWork:
    def __init__(self, log):
        self.commits = 0
        self._log = log

    def commit(self):
        self._log.append("commit")
        self.commits += 1

    def rollback(self):
        pass


def _connection(id_, workspace_id, source_type, account):
    return SourceConnection(
        id=id_, user_id=1, workspace_id=workspace_id, source_type=source_type,
        external_account=account, status=ConnectionStatus.ACTIVE, sync_cursor=None,
        last_synced_at=None, last_error=None, created_at=NOW,
    )


def _upload(id_, workspace_id):
    return UploadedFile(
        id=id_, user_id=1, workspace_id=workspace_id, original_filename=f"f{id_}.txt",
        mime_type="text/plain", file_size_bytes=1, storage_path=f"1/f{id_}.txt",
        content_sha256="a" * 64, document_version=1, status=UploadStatus.READY,
        document_id=None, error=None, attempts=0, created_at=NOW, updated_at=NOW,
    )


class DeleteEnv:
    def __init__(self, revoke_fails=False):
        self.log: list[str] = []
        self.workspaces = FakeWorkspaceRepository()
        self.a = self.workspaces.create(1, "Client A")
        self.b = self.workspaces.create(1, "Client B")
        self.connections = FakeConnections([
            _connection(1, self.a.id, SourceType.GMAIL, "a@gmail.com"),
            _connection(2, self.a.id, SourceType.FILE, None),
            _connection(3, self.b.id, SourceType.GMAIL, "b@gmail.com"),
        ])
        self.credentials = FakeCredentials({
            1: Credentials("ta", "ra", NOW), 3: Credentials("tb", "rb", NOW),
        })
        self.oauth = FakeOAuthProvider(fail=revoke_fails)
        self.uploads = FakeUploads([_upload(10, self.a.id), _upload(11, self.b.id)])
        self.documents = FakeDocuments()
        self.search_index = FakeSearchIndex(self.log)
        self.storage = FakeStorage(self.log)
        self.uow = FakeUnitOfWork(self.log)

    def delete(self, workspace_id, user_id=1):
        DeleteWorkspace(
            self.workspaces, self.connections, self.credentials, lambda _type: self.oauth,
            self.uploads, self.documents, self.search_index, self.storage, self.uow,
        ).execute(workspace_id, user_id)


def test_delete_workspace_removes_everything_in_it_and_nothing_else():
    env = DeleteEnv()

    env.delete(env.a.id)

    assert env.a.id not in env.workspaces.rows and env.b.id in env.workspaces.rows
    assert sorted(env.connections.deleted) == [1, 2]
    assert list(env.connections.rows) == [3]
    # Gmail access revoked; the uploads connection has nothing to revoke.
    assert env.oauth.revoked == [Credentials("ta", "ra", NOW)]
    assert list(env.credentials.creds) == [3]
    assert list(env.uploads.rows) == [11]
    assert env.documents.deleted_workspaces == [env.a.id]
    assert env.search_index.deleted_workspaces == [env.a.id]
    assert env.storage.deleted == ["1/f10.txt"]
    # Files and index entries go only after the Postgres commit.
    assert env.log == ["commit", "file", "index"]


def test_delete_continues_when_revoking_fails():
    env = DeleteEnv(revoke_fails=True)

    env.delete(env.a.id)

    assert env.a.id not in env.workspaces.rows
    assert sorted(env.connections.deleted) == [1, 2]


def test_delete_someone_elses_workspace_is_not_found_and_changes_nothing():
    env = DeleteEnv()

    with pytest.raises(WorkspaceNotFound):
        env.delete(env.a.id, user_id=2)

    assert env.a.id in env.workspaces.rows
    assert env.connections.deleted == []
    assert env.log == []
