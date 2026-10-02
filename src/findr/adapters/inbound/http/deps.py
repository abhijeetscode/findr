from collections.abc import Iterator

from elasticsearch import Elasticsearch
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from findr.adapters.outbound.postgres.session_store_postgres import SessionStorePostgres
from findr.adapters.outbound.postgres.user_repository_postgres import UserRepositoryPostgres
from findr.adapters.outbound.elasticsearch.search_index_elasticsearch import ElasticsearchIndex
from findr.adapters.outbound.files.local_file_storage import LocalFileStorage
from findr.config import Settings
from findr.domain.entities import User, Workspace
from findr.observability import add_fields
from findr.ports.upload_queue import UploadQueue

SESSION_COOKIE_NAME = "findr_session"


def get_db_session(request: Request) -> Iterator[Session]:
    session_factory = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_es_client(request: Request) -> Elasticsearch:
    return request.app.state.es_client


def get_search_index(
    request: Request,
    es_client: Elasticsearch = Depends(get_es_client),
    settings: Settings = Depends(get_settings),
) -> ElasticsearchIndex:
    return ElasticsearchIndex(
        es_client,
        settings.elasticsearch_index,
        request.app.state.embedding_provider,
        min_similarity=settings.semantic_min_similarity,
    )


def get_upload_queue(request: Request) -> UploadQueue:
    return request.app.state.upload_queue


def get_file_storage(settings: Settings = Depends(get_settings)) -> LocalFileStorage:
    return LocalFileStorage(settings.upload_storage_root)


def get_current_user(request: Request, db: Session = Depends(get_db_session)) -> User:
    """Dependency for any route that requires a logged-in user (e.g. the
    Gmail-connect and search routers added in later steps)."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user_id = SessionStorePostgres(db).get_user_id(token) if token else None
    user = UserRepositoryPostgres(db).get_by_id(user_id) if user_id is not None else None
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    # On every log line for the rest of this request (specs/logging-telemetry.md §5).
    add_fields(user_id=user.id)
    return user


def get_workspace(
    workspace_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
) -> Workspace:
    """Every /workspaces/{workspace_id}/... route depends on this: the id from
    the URL is only trusted once loaded for the logged-in user. 404 alike for
    "doesn't exist" and "someone else's" (specs/workspaces.md §2)."""
    from findr.adapters.outbound.postgres.workspace_repository_postgres import (
        WorkspaceRepositoryPostgres,
    )
    from findr.application.workspaces.manage_workspaces import GetWorkspace
    from findr.domain.exceptions import WorkspaceNotFound

    try:
        workspace = GetWorkspace(WorkspaceRepositoryPostgres(db)).execute(workspace_id, user.id)
    except WorkspaceNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found") from exc
    add_fields(workspace_id=workspace.id)
    return workspace
