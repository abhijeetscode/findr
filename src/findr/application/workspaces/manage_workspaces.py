import logging

from findr.domain.entities import Workspace
from findr.domain.exceptions import DuplicateWorkspaceName, InvalidWorkspaceName, WorkspaceNotFound
from findr.observability import log_event
from findr.ports.workspace_repository import WorkspaceRepository

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 100


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise InvalidWorkspaceName("Workspace name is required")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise InvalidWorkspaceName(f"Workspace name must be at most {MAX_NAME_LENGTH} characters")
    return cleaned


class GetWorkspace:
    """The one entry point every workspace-scoped request goes through: a
    workspace id from a request is never trusted until it's been loaded for
    this user (specs/workspaces.md §2)."""

    def __init__(self, workspaces: WorkspaceRepository) -> None:
        self._workspaces = workspaces

    def execute(self, workspace_id: int, user_id: int) -> Workspace:
        workspace = self._workspaces.get(workspace_id, user_id)
        if workspace is None:
            raise WorkspaceNotFound(f"No workspace {workspace_id} for this user")
        return workspace


class ListWorkspaces:
    def __init__(self, workspaces: WorkspaceRepository) -> None:
        self._workspaces = workspaces

    def execute(self, user_id: int) -> list[Workspace]:
        return self._workspaces.list_for_user(user_id)


class CreateWorkspace:
    def __init__(self, workspaces: WorkspaceRepository) -> None:
        self._workspaces = workspaces

    def execute(self, user_id: int, name: str) -> Workspace:
        name = _clean_name(name)
        if self._workspaces.get_by_name(user_id, name) is not None:
            raise DuplicateWorkspaceName(f"You already have a workspace named {name!r}")
        workspace = self._workspaces.create(user_id, name)
        # The id only: a workspace's name is the client's name
        # (specs/logging-telemetry.md §8).
        log_event(logger, "workspace.created", workspace_id=workspace.id)
        return workspace


class RenameWorkspace:
    def __init__(self, workspaces: WorkspaceRepository) -> None:
        self._workspaces = workspaces

    def execute(self, workspace_id: int, user_id: int, name: str) -> Workspace:
        workspace = GetWorkspace(self._workspaces).execute(workspace_id, user_id)
        name = _clean_name(name)
        clash = self._workspaces.get_by_name(user_id, name)
        if clash is not None and clash.id != workspace_id:
            raise DuplicateWorkspaceName(f"You already have a workspace named {name!r}")
        if name != workspace.name:
            self._workspaces.rename(workspace_id, name)
            workspace.name = name
            log_event(logger, "workspace.renamed", workspace_id=workspace_id)
        return workspace
