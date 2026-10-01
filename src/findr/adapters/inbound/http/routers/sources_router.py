from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from findr.adapters.inbound.http.deps import (
    get_current_user,
    get_db_session,
    get_file_storage,
    get_search_index,
    get_settings,
    get_upload_queue,
)
from findr.adapters.outbound.connector_factory import connector_for, oauth_provider_for
from findr.adapters.outbound.crypto.token_cipher import TokenCipher
from findr.adapters.outbound.elasticsearch.search_index_elasticsearch import ElasticsearchIndex
from findr.adapters.outbound.postgres.credential_store_postgres import CredentialStorePostgres
from findr.adapters.outbound.postgres.document_repository_postgres import (
    DocumentRepositoryPostgres,
)
from findr.adapters.outbound.files.local_file_storage import LocalFileStorage
from findr.adapters.outbound.files.mime_types import SUPPORTED_MIME_TYPES, resolve_mime_type
from findr.adapters.outbound.postgres.oauth_state_repository_postgres import (
    OAuthStateRepositoryPostgres,
)
from findr.adapters.outbound.postgres.source_connection_repo_postgres import (
    SourceConnectionRepositoryPostgres,
)
from findr.adapters.outbound.postgres.unit_of_work_postgres import UnitOfWorkPostgres
from findr.adapters.outbound.postgres.uploaded_file_repository_postgres import (
    UploadedFileRepositoryPostgres,
)
from findr.adapters.outbound.system_clock import SystemClock
from findr.application.sources.connect_gmail import BeginGmailConnect, CompleteGmailConnect
from findr.application.sources.connect_notion import BeginNotionConnect, CompleteNotionConnect
from findr.application.sources.connect_slack import BeginSlackConnect, CompleteSlackConnect
from findr.application.sources.disconnect_source import DisconnectSource
from findr.application.sources.list_connections import ListConnections
from findr.application.sync.sync_source import SyncSource
from findr.application.uploads.upload_file import UploadFile as UploadFileUseCase
from findr.config import Settings
from findr.domain.entities import SourceConnection, User
from findr.domain.exceptions import (
    ConnectionNotFound,
    ExtractionFailed,
    InvalidOAuthState,
    SourceAuthError,
    UnsupportedFileType,
)
from findr.domain.value_objects import ConnectionStatus, SourceType
from findr.ports.upload_queue import UploadQueue

router = APIRouter(prefix="/sources", tags=["sources"])


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


@router.get("")
def list_sources(
    user: User = Depends(get_current_user), db: Session = Depends(get_db_session)
) -> list[SourceConnectionResponse]:
    connections = ListConnections(SourceConnectionRepositoryPostgres(db)).execute(user.id)
    return [_to_response(c) for c in connections]


class UploadResponse(BaseModel):
    upload_id: int
    filename: str
    mime_type: str
    file_size_bytes: int
    status: str


@router.post("/upload", status_code=status.HTTP_202_ACCEPTED, response_model=UploadResponse)
async def upload_file(
    file: UploadFile,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
    file_storage: LocalFileStorage = Depends(get_file_storage),
    upload_queue: UploadQueue = Depends(get_upload_queue),
) -> UploadResponse:
    """Stores one file and queues it for background processing (OCR,
    tables, chunking, indexing) — see specs/upload-chunking.md. Track it
    with GET /uploads/{upload_id}."""
    filename = file.filename or "untitled"
    content = await file.read()
    use_case = UploadFileUseCase(
        SUPPORTED_MIME_TYPES,
        file_storage,
        UploadedFileRepositoryPostgres(db),
        UnitOfWorkPostgres(db),
        upload_queue,
    )
    try:
        # In a thread: the queue adapter hands its coroutine to this event
        # loop and waits for it, which would deadlock on the loop itself.
        upload = await run_in_threadpool(
            use_case.execute,
            user.id,
            filename,
            resolve_mime_type(filename, file.content_type),
            content,
        )
    except (UnsupportedFileType, ExtractionFailed) as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return UploadResponse(
        upload_id=upload.id,
        filename=upload.original_filename,
        mime_type=upload.mime_type,
        file_size_bytes=upload.file_size_bytes,
        status=upload.status.value,
    )


@router.get("/gmail/connect")
def gmail_connect(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    use_case = BeginGmailConnect(
        oauth_provider_for(SourceType.GMAIL, settings), OAuthStateRepositoryPostgres(db)
    )
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
        oauth_provider_for(SourceType.GMAIL, settings),
        OAuthStateRepositoryPostgres(db),
        SourceConnectionRepositoryPostgres(db),
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
    use_case = BeginSlackConnect(
        oauth_provider_for(SourceType.SLACK, settings), OAuthStateRepositoryPostgres(db)
    )
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
        oauth_provider_for(SourceType.SLACK, settings),
        OAuthStateRepositoryPostgres(db),
        SourceConnectionRepositoryPostgres(db),
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
    use_case = BeginNotionConnect(
        oauth_provider_for(SourceType.NOTION, settings), OAuthStateRepositoryPostgres(db)
    )
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
        oauth_provider_for(SourceType.NOTION, settings),
        OAuthStateRepositoryPostgres(db),
        SourceConnectionRepositoryPostgres(db),
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
