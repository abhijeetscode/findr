from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from findr.adapters.inbound.http.deps import (
    get_current_user,
    get_db_session,
    get_file_storage,
    get_search_index,
    get_settings,
)
from findr.adapters.outbound.connector_factory import oauth_provider_for
from findr.adapters.outbound.crypto.token_cipher import TokenCipher
from findr.adapters.outbound.elasticsearch.search_index_elasticsearch import ElasticsearchIndex
from findr.adapters.outbound.files.local_file_storage import LocalFileStorage
from findr.adapters.outbound.postgres.credential_store_postgres import CredentialStorePostgres
from findr.adapters.outbound.postgres.document_repository_postgres import (
    DocumentRepositoryPostgres,
)
from findr.adapters.outbound.postgres.source_connection_repo_postgres import (
    SourceConnectionRepositoryPostgres,
)
from findr.adapters.outbound.postgres.unit_of_work_postgres import UnitOfWorkPostgres
from findr.adapters.outbound.postgres.uploaded_file_repository_postgres import (
    UploadedFileRepositoryPostgres,
)
from findr.adapters.outbound.postgres.workspace_repository_postgres import (
    WorkspaceRepositoryPostgres,
)
from findr.application.workspaces.delete_workspace import DeleteWorkspace
from findr.application.workspaces.manage_workspaces import (
    CreateWorkspace,
    ListWorkspaces,
    RenameWorkspace,
)
from findr.config import Settings
from findr.domain.entities import User, Workspace
from findr.domain.exceptions import (
    DuplicateWorkspaceName,
    InvalidWorkspaceName,
    WorkspaceNotFound,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


class WorkspaceRequest(BaseModel):
    name: str


class WorkspaceResponse(BaseModel):
    id: int
    name: str
    created_at: datetime


def _to_response(workspace: Workspace) -> WorkspaceResponse:
    return WorkspaceResponse(id=workspace.id, name=workspace.name, created_at=workspace.created_at)


def _name_error(exc: Exception) -> HTTPException:
    if isinstance(exc, DuplicateWorkspaceName):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


@router.get("", response_model=list[WorkspaceResponse])
def list_workspaces(
    user: User = Depends(get_current_user), db: Session = Depends(get_db_session)
) -> list[WorkspaceResponse]:
    return [_to_response(w) for w in ListWorkspaces(WorkspaceRepositoryPostgres(db)).execute(user.id)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=WorkspaceResponse)
def create_workspace(
    body: WorkspaceRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
) -> WorkspaceResponse:
    try:
        workspace = CreateWorkspace(WorkspaceRepositoryPostgres(db)).execute(user.id, body.name)
    except (DuplicateWorkspaceName, InvalidWorkspaceName) as exc:
        db.rollback()
        raise _name_error(exc) from exc
    db.commit()
    return _to_response(workspace)


@router.patch("/{workspace_id}", response_model=WorkspaceResponse)
def rename_workspace(
    workspace_id: int,
    body: WorkspaceRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
) -> WorkspaceResponse:
    try:
        workspace = RenameWorkspace(WorkspaceRepositoryPostgres(db)).execute(
            workspace_id, user.id, body.name
        )
    except WorkspaceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found") from exc
    except (DuplicateWorkspaceName, InvalidWorkspaceName) as exc:
        db.rollback()
        raise _name_error(exc) from exc
    db.commit()
    return _to_response(workspace)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workspace(
    workspace_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    search_index: ElasticsearchIndex = Depends(get_search_index),
    file_storage: LocalFileStorage = Depends(get_file_storage),
) -> None:
    """Deletes the workspace and everything in it — its Gmail connections
    (access revoked), uploads, stored files and search entries. See
    specs/workspaces.md §4."""
    use_case = DeleteWorkspace(
        WorkspaceRepositoryPostgres(db),
        SourceConnectionRepositoryPostgres(db),
        CredentialStorePostgres(db, TokenCipher(settings.token_encryption_key)),
        lambda source_type: oauth_provider_for(source_type, settings),
        UploadedFileRepositoryPostgres(db),
        DocumentRepositoryPostgres(db),
        search_index,
        file_storage,
        UnitOfWorkPostgres(db),
    )
    try:
        use_case.execute(workspace_id, user.id)
    except WorkspaceNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found") from exc
