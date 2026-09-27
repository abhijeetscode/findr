from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from findr.adapters.inbound.http.deps import get_current_user, get_db_session, get_settings
from findr.adapters.outbound.crypto.token_cipher import TokenCipher
from findr.adapters.outbound.gmail.gmail_oauth_provider import GmailOAuthProvider
from findr.adapters.outbound.notion.notion_oauth_provider import NotionOAuthProvider
from findr.adapters.outbound.slack.slack_oauth_provider import SlackOAuthProvider
from findr.adapters.outbound.sqlite.credential_store_sqlite import CredentialStoreSqlite
from findr.adapters.outbound.sqlite.oauth_state_repository_sqlite import (
    OAuthStateRepositorySqlite,
)
from findr.adapters.outbound.sqlite.source_connection_repo_sqlite import (
    SourceConnectionRepositorySqlite,
)
from findr.application.sources.connect_gmail import BeginGmailConnect, CompleteGmailConnect
from findr.application.sources.connect_notion import BeginNotionConnect, CompleteNotionConnect
from findr.application.sources.connect_slack import BeginSlackConnect, CompleteSlackConnect
from findr.application.sources.disconnect_source import DisconnectSource
from findr.application.sources.list_connections import ListConnections
from findr.config import Settings
from findr.domain.entities import SourceConnection, User
from findr.domain.exceptions import ConnectionNotFound, InvalidOAuthState, SourceAuthError

router = APIRouter(prefix="/sources", tags=["sources"])


def _gmail_oauth_provider(settings: Settings) -> GmailOAuthProvider:
    return GmailOAuthProvider(
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
        redirect_uri=settings.google_oauth_redirect_uri,
    )


def _slack_oauth_provider(settings: Settings) -> SlackOAuthProvider:
    return SlackOAuthProvider(
        client_id=settings.slack_client_id,
        client_secret=settings.slack_client_secret,
        redirect_uri=settings.slack_redirect_uri,
    )


def _notion_oauth_provider(settings: Settings) -> NotionOAuthProvider:
    return NotionOAuthProvider(
        client_id=settings.notion_client_id,
        client_secret=settings.notion_client_secret,
        redirect_uri=settings.notion_redirect_uri,
    )


# Per-connection: which provider disconnect() needs to revoke against, keyed
# by the same source_type value stored on the connection.
def _oauth_provider_for(source_type: str, settings: Settings):
    return {
        "gmail": _gmail_oauth_provider,
        "slack": _slack_oauth_provider,
        "notion": _notion_oauth_provider,
    }[source_type](settings)


def _credential_store(db: Session, settings: Settings) -> CredentialStoreSqlite:
    return CredentialStoreSqlite(db, TokenCipher(settings.token_encryption_key))


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
    use_case = BeginGmailConnect(_gmail_oauth_provider(settings), OAuthStateRepositorySqlite(db))
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
        _gmail_oauth_provider(settings),
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
    return RedirectResponse("/", status_code=status.HTTP_302_FOUND)


@router.get("/slack/connect")
def slack_connect(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    use_case = BeginSlackConnect(_slack_oauth_provider(settings), OAuthStateRepositorySqlite(db))
    authorize_url = use_case.execute(user.id)
    db.commit()
    return RedirectResponse(authorize_url, status_code=status.HTTP_302_FOUND)


@router.get("/slack/callback")
def slack_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    # Deliberately NOT behind get_current_user — same reasoning as Gmail's
    # callback above.
    use_case = CompleteSlackConnect(
        _slack_oauth_provider(settings),
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
    return RedirectResponse("/", status_code=status.HTTP_302_FOUND)


@router.get("/notion/connect")
def notion_connect(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    use_case = BeginNotionConnect(_notion_oauth_provider(settings), OAuthStateRepositorySqlite(db))
    authorize_url = use_case.execute(user.id)
    db.commit()
    return RedirectResponse(authorize_url, status_code=status.HTTP_302_FOUND)


@router.get("/notion/callback")
def notion_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    # Deliberately NOT behind get_current_user — same reasoning as Gmail's
    # callback above.
    use_case = CompleteNotionConnect(
        _notion_oauth_provider(settings),
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
    return RedirectResponse("/", status_code=status.HTTP_302_FOUND)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def disconnect(
    connection_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> None:
    connection_repo = SourceConnectionRepositorySqlite(db)
    connection = connection_repo.get(connection_id, user.id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")

    use_case = DisconnectSource(
        connection_repo,
        _credential_store(db, settings),
        _oauth_provider_for(connection.source_type.value, settings),
    )
    try:
        use_case.execute(connection_id, user.id)
    except ConnectionNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    db.commit()
