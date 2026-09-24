from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from findr.adapters.inbound.http.deps import get_current_user, get_db_session, get_settings
from findr.adapters.outbound.crypto.token_cipher import TokenCipher
from findr.adapters.outbound.gmail.gmail_oauth_provider import GmailOAuthProvider
from findr.adapters.outbound.sqlite.credential_store_sqlite import CredentialStoreSqlite
from findr.adapters.outbound.sqlite.oauth_state_repository_sqlite import (
    OAuthStateRepositorySqlite,
)
from findr.adapters.outbound.sqlite.source_connection_repo_sqlite import (
    SourceConnectionRepositorySqlite,
)
from findr.application.sources.connect_gmail import BeginGmailConnect, CompleteGmailConnect
from findr.application.sources.disconnect_source import DisconnectSource
from findr.application.sources.list_connections import ListConnections
from findr.config import Settings
from findr.domain.entities import SourceConnection, User
from findr.domain.exceptions import ConnectionNotFound, InvalidOAuthState, SourceAuthError

router = APIRouter(prefix="/sources", tags=["sources"])


def _oauth_provider(settings: Settings) -> GmailOAuthProvider:
    return GmailOAuthProvider(
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
        redirect_uri=settings.google_oauth_redirect_uri,
    )


def _credential_store(db: Session, settings: Settings) -> CredentialStoreSqlite:
    return CredentialStoreSqlite(db, TokenCipher(settings.token_encryption_key))


class SourceConnectionResponse(BaseModel):
    id: int
    source_type: str
    external_account: str | None
    status: str
    last_synced_at: str | None
    last_error: str | None


def _to_response(connection: SourceConnection) -> SourceConnectionResponse:
    return SourceConnectionResponse(
        id=connection.id,
        source_type=connection.source_type.value,
        external_account=connection.external_account,
        status=connection.status.value,
        last_synced_at=(
            connection.last_synced_at.isoformat() if connection.last_synced_at else None
        ),
        last_error=connection.last_error,
    )


@router.get("")
def list_sources(
    user: User = Depends(get_current_user), db: Session = Depends(get_db_session)
) -> list[SourceConnectionResponse]:
    connections = ListConnections(SourceConnectionRepositorySqlite(db)).execute(user.id)
    return [_to_response(c) for c in connections]


@router.get("/gmail/connect")
def gmail_connect(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    use_case = BeginGmailConnect(_oauth_provider(settings), OAuthStateRepositorySqlite(db))
    authorize_url = use_case.execute(user.id)
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
        _oauth_provider(settings),
        OAuthStateRepositorySqlite(db),
        SourceConnectionRepositorySqlite(db),
        _credential_store(db, settings),
    )
    try:
        use_case.execute(code, state)
    except InvalidOAuthState as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SourceAuthError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse("/sources", status_code=status.HTTP_302_FOUND)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def disconnect(
    connection_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> None:
    use_case = DisconnectSource(
        SourceConnectionRepositorySqlite(db),
        _credential_store(db, settings),
        _oauth_provider(settings),
    )
    try:
        use_case.execute(connection_id, user.id)
    except ConnectionNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    db.commit()
