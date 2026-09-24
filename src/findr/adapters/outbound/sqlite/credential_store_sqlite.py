from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session as DbSession

from findr.adapters.outbound.crypto.token_cipher import TokenCipher
from findr.adapters.outbound.sqlite.models import SourceConnectionModel
from findr.domain.entities import Credentials


class CredentialStoreSqlite:
    """Implements ports.credential_store.CredentialStore. Tokens are
    encrypted at rest via TokenCipher (Fernet) — see specs/gmail-connector.md
    for why this is column-level, not full-disk, encryption."""

    def __init__(self, db: DbSession, cipher: TokenCipher) -> None:
        self._db = db
        self._cipher = cipher

    def save(
        self, connection_id: int, access_token: str, refresh_token: str, expires_at: datetime
    ) -> None:
        row = self._db.get(SourceConnectionModel, connection_id)
        if row is None:
            raise ValueError(f"No source connection with id {connection_id!r}")
        row.access_token_enc = self._cipher.encrypt(access_token)
        row.refresh_token_enc = self._cipher.encrypt(refresh_token)
        row.token_expires_at = expires_at
        self._db.flush()

    def get(self, connection_id: int) -> Credentials | None:
        row = self._db.get(SourceConnectionModel, connection_id)
        if row is None or row.access_token_enc is None or row.refresh_token_enc is None:
            return None
        return Credentials(
            access_token=self._cipher.decrypt(row.access_token_enc),
            refresh_token=self._cipher.decrypt(row.refresh_token_enc),
            expires_at=row.token_expires_at,
        )

    def delete(self, connection_id: int) -> None:
        row = self._db.get(SourceConnectionModel, connection_id)
        if row is None:
            return
        row.access_token_enc = None
        row.refresh_token_enc = None
        row.token_expires_at = None
        self._db.flush()
