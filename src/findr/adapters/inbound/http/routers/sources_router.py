from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from findr.adapters.inbound.http.deps import (
    get_current_user,
    get_db_session,
    get_search_index,
    get_settings,
    get_workspace,
)
from findr.adapters.outbound.connector_factory import connector_for, oauth_provider_for
from findr.adapters.outbound.crypto.token_cipher import TokenCipher
from findr.adapters.outbound.elasticsearch.search_index_elasticsearch import ElasticsearchIndex
from findr.adapters.outbound.postgres.credential_store_postgres import CredentialStorePostgres
from findr.adapters.outbound.postgres.document_repository_postgres import (
    DocumentRepositoryPostgres,
)
from findr.adapters.outbound.postgres.oauth_state_repository_postgres import (
    OAuthStateRepositoryPostgres,
)
from findr.adapters.outbound.postgres.source_connection_repo_postgres import (
    SourceConnectionRepositoryPostgres,
)
from findr.adapters.outbound.postgres.workspace_repository_postgres import (
    WorkspaceRepositoryPostgres,
)
from findr.adapters.outbound.system_clock import SystemClock
from findr.application.sources.connect_gmail import BeginGmailConnect, CompleteGmailConnect
from findr.application.sources.disconnect_source import DisconnectSource
from findr.application.sources.list_connections import ListConnections
from findr.application.sync.sync_source import SyncSource
from findr.config import Settings
from findr.domain.entities import SourceConnection, User, Workspace
from findr.domain.exceptions import (
    ConnectionNotFound,
    InvalidOAuthState,
    SourceAuthError,
    SourceInOtherWorkspace,
)
from findr.domain.value_objects import ConnectionStatus, SourceType

# Item routes: a connection's id already pins down its workspace.
router = APIRouter(prefix="/sources", tags=["sources"])
# Workspace-scoped routes (specs/workspaces.md §6).
workspace_router = APIRouter(prefix="/workspaces/{workspace_id}/sources", tags=["sources"])


def _credential_store(db: Session, settings: Settings) -> CredentialStorePostgres:
    return CredentialStorePostgres(db, TokenCipher(settings.token_encryption_key))


class SourceConnectionResponse(BaseModel):
    id: int
    source_type: str
    external_account: str | None
    display_name: str | None
    status: str
    last_synced_at: str | None
    last_error: str | None


def _to_response(connection: SourceConnection) -> SourceConnectionResponse:
    return SourceConnectionResponse(
        id=connection.id,
        source_type=connection.source_type.value,
        external_account=connection.external_account,
        # Falls back to external_account for connectors where it's already
        # friendly (Gmail's email) or for rows created before this field
        # existed.
        display_name=connection.display_name or connection.external_account,
        status=connection.status.value,
        last_synced_at=(
            connection.last_synced_at.isoformat() if connection.last_synced_at else None
        ),
        last_error=connection.last_error,
    )


@workspace_router.get("")
def list_sources(
    workspace: Workspace = Depends(get_workspace), db: Session = Depends(get_db_session)
) -> list[SourceConnectionResponse]:
    """The workspace's connections (Gmail accounts, "Uploaded files")."""
    connections = ListConnections(SourceConnectionRepositoryPostgres(db)).execute(workspace.id)
    return [_to_response(c) for c in connections]


@workspace_router.get("/gmail/connect")
def gmail_connect(
    workspace: Workspace = Depends(get_workspace),
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Starts connecting a Gmail account into this workspace."""
    use_case = BeginGmailConnect(
        oauth_provider_for(SourceType.GMAIL, settings), OAuthStateRepositoryPostgres(db)
    )
    authorize_url = use_case.execute(workspace.user_id, workspace.id)
    db.commit()
    return RedirectResponse(authorize_url, status_code=status.HTTP_302_FOUND)


@router.get("/gmail/callback")
def gmail_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    # Deliberately NOT behind get_current_user: the connection belongs to
    # whoever initiated the connect (bound to `state`), not to whatever
    # session cookie happens to be current when Google redirects back.
    use_case = CompleteGmailConnect(
        oauth_provider_for(SourceType.GMAIL, settings),
        OAuthStateRepositoryPostgres(db),
        SourceConnectionRepositoryPostgres(db),
        _credential_store(db, settings),
        WorkspaceRepositoryPostgres(db),
    )
    try:
        use_case.execute(code, state)
    except SourceInOtherWorkspace as exc:
        # A browser redirect, not an API call: send the user back to the UI
        # with a message it shows (specs/workspaces.md §5.2).
        db.rollback()
        return RedirectResponse(
            "/?" + urlencode({"connect_error": str(exc)}), status_code=status.HTTP_302_FOUND
        )
    except InvalidOAuthState as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SourceAuthError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse("/", status_code=status.HTTP_302_FOUND)


@router.post("/{connection_id}/sync", response_model=SourceConnectionResponse)
def resync(
    connection_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    search_index: ElasticsearchIndex = Depends(get_search_index),
) -> SourceConnectionResponse:
    """Manually triggers an immediate sync for one connection, instead of
    waiting for the next scheduler tick (up to FINDR_SYNC_INTERVAL_SECONDS
    away) — same SyncSource the background scheduler uses, just run
    synchronously for one connection on request."""
    connection_repo = SourceConnectionRepositoryPostgres(db)
    connection = connection_repo.get(connection_id, user.id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    if connection.status == ConnectionStatus.DISCONNECTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connection is disconnected; reconnect it first",
        )
    if connection.source_type == SourceType.FILE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Uploaded files have nothing to sync; upload a new file instead",
        )

    use_case = SyncSource(
        connector_for(connection.source_type, connection.user_id, connection.id),
        oauth_provider_for(connection.source_type, settings),
        _credential_store(db, settings),
        connection_repo,
        DocumentRepositoryPostgres(db),
        search_index,
        SystemClock(),
    )
    use_case.execute(connection)
    db.commit()

    updated = connection_repo.get(connection_id, user.id)
    assert updated is not None  # just synced above; a miss here means a real bug
    return _to_response(updated)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def disconnect(
    connection_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> None:
    connection_repo = SourceConnectionRepositoryPostgres(db)
    connection = connection_repo.get(connection_id, user.id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    if connection.source_type == SourceType.FILE:
        # No OAuth grant to revoke; per-file removal is DELETE /documents/{id}
        # (specs/file-upload.md §2.3).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Uploaded files can't be disconnected; delete individual files instead",
        )

    use_case = DisconnectSource(
        connection_repo,
        _credential_store(db, settings),
        oauth_provider_for(connection.source_type, settings),
    )
    try:
        use_case.execute(connection_id, user.id)
    except ConnectionNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    db.commit()
