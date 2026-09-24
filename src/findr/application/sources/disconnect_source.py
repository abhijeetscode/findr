from findr.domain.exceptions import ConnectionNotFound
from findr.domain.value_objects import ConnectionStatus
from findr.ports.credential_store import CredentialStore
from findr.ports.oauth_provider import OAuthProvider
from findr.ports.source_connection_repo import SourceConnectionRepository


class DisconnectSource:
    def __init__(
        self,
        connection_repo: SourceConnectionRepository,
        credential_store: CredentialStore,
        oauth_provider: OAuthProvider,
    ) -> None:
        self._connection_repo = connection_repo
        self._credential_store = credential_store
        self._oauth_provider = oauth_provider

    def execute(self, connection_id: int, user_id: int) -> None:
        connection = self._connection_repo.get(connection_id, user_id)
        if connection is None:
            raise ConnectionNotFound(f"No connection {connection_id} for this user")

        credentials = self._credential_store.get(connection_id)
        if credentials is not None:
            self._oauth_provider.revoke(credentials.refresh_token)
        self._credential_store.delete(connection_id)
        self._connection_repo.update_status(connection_id, ConnectionStatus.DISCONNECTED)
