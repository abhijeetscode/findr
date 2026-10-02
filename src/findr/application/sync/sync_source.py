import logging
import time

from findr.domain.entities import SourceConnection
from findr.domain.exceptions import SourceAuthError, SourceCursorExpired
from findr.domain.value_objects import ConnectionStatus
from findr.observability import bind, log_event
from findr.observability.events import elapsed_ms
from findr.ports.clock import Clock
from findr.ports.credential_store import CredentialStore
from findr.ports.document_repository import DocumentRepository
from findr.ports.oauth_provider import OAuthProvider
from findr.ports.search_index import SearchIndex
from findr.ports.source_connection_repo import SourceConnectionRepository
from findr.ports.source_connector import SourceConnector


logger = logging.getLogger(__name__)


class SyncSource:
    """Generic over SourceConnector — works for Gmail now, any future
    connector (Google Drive, ...) unchanged, as long as that connector is
    also bound to a specific connection like GmailConnector is.

    Never raises: one connection's failure is caught and recorded on that
    connection's status, so a scheduler tick over many connections isn't
    aborted by a single bad one.
    """

    def __init__(
        self,
        connector: SourceConnector,
        oauth_provider: OAuthProvider,
        credential_store: CredentialStore,
        connection_repo: SourceConnectionRepository,
        document_repo: DocumentRepository,
        search_index: SearchIndex,
        clock: Clock,
    ) -> None:
        self._connector = connector
        self._oauth_provider = oauth_provider
        self._credential_store = credential_store
        self._connection_repo = connection_repo
        self._document_repo = document_repo
        self._search_index = search_index
        self._clock = clock

    def execute(self, connection: SourceConnection) -> ConnectionStatus:
        """Returns the status the connection was left in."""
        with bind(
            connection_id=connection.id,
            user_id=connection.user_id,
            workspace_id=connection.workspace_id,
        ):
            return self._execute(connection)

    def _execute(self, connection: SourceConnection) -> ConnectionStatus:
        start = time.perf_counter()
        event = {
            "source_type": connection.source_type.value,
            "credentials_refreshed": False,
            "full_resync": False,
        }
        try:
            credentials = self._credential_store.get(connection.id)
            if credentials is None:
                raise SourceAuthError(f"No stored credentials for connection {connection.id}")

            if credentials.expires_at <= self._clock.now():
                credentials = self._oauth_provider.refresh(credentials.refresh_token)
                self._credential_store.save(
                    connection.id,
                    credentials.access_token,
                    credentials.refresh_token,
                    credentials.expires_at,
                )
                event["credentials_refreshed"] = True

            step = time.perf_counter()
            try:
                batch = self._connector.fetch_changes(credentials, connection.sync_cursor)
            except SourceCursorExpired:
                # Cursor too old to resume from — fall back to a full
                # resync, same as a brand-new connection.
                event["full_resync"] = True
                batch = self._connector.fetch_changes(credentials, None)
            event["fetch_ms"] = elapsed_ms(step)

            step = time.perf_counter()
            if batch.upserts:
                persisted = self._document_repo.upsert_many(batch.upserts)
                self._search_index.index_documents(
                    persisted,
                    connection.source_type,
                    connection.external_account,
                    connection.workspace_id,
                )
            if batch.deleted_external_ids:
                self._document_repo.delete_many(connection.id, batch.deleted_external_ids)
                self._search_index.delete_documents(connection.id, batch.deleted_external_ids)
            event["index_ms"] = elapsed_ms(step)

            self._connection_repo.update_cursor(connection.id, batch.new_cursor, self._clock.now())
            self._connection_repo.update_status(connection.id, ConnectionStatus.ACTIVE)
            log_event(
                logger,
                "sync.connection",
                outcome="ok",
                upserts=len(batch.upserts),
                deletes=len(batch.deleted_external_ids),
                duration_ms=elapsed_ms(start),
                **event,
            )
            return ConnectionStatus.ACTIVE

        except SourceAuthError as exc:
            self._connection_repo.update_status(
                connection.id, ConnectionStatus.NEEDS_REAUTH, last_error=str(exc)
            )
            # Expected when a grant is revoked or expires: no traceback.
            log_event(
                logger,
                "sync.connection",
                level=logging.WARNING,
                outcome="needs_reauth",
                duration_ms=elapsed_ms(start),
                **event,
            )
            return ConnectionStatus.NEEDS_REAUTH
        except Exception as exc:  # noqa: BLE001 - isolates this connection's failure
            self._connection_repo.update_status(
                connection.id, ConnectionStatus.ERROR, last_error=str(exc)
            )
            log_event(
                logger,
                "sync.connection",
                level=logging.WARNING,
                exc_info=True,
                outcome="error",
                duration_ms=elapsed_ms(start),
                **event,
            )
            return ConnectionStatus.ERROR
