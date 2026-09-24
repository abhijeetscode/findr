from datetime import datetime
from typing import Protocol

from findr.domain.entities import Credentials


class CredentialStore(Protocol):
    def save(
        self, connection_id: int, access_token: str, refresh_token: str, expires_at: datetime
    ) -> None: ...
    def get(self, connection_id: int) -> Credentials | None: ...
    def delete(self, connection_id: int) -> None: ...
