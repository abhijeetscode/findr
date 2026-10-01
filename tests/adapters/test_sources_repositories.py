from datetime import datetime

from cryptography.fernet import Fernet

from findr.adapters.outbound.crypto.token_cipher import TokenCipher
from findr.adapters.outbound.postgres.credential_store_postgres import CredentialStorePostgres
from findr.adapters.outbound.postgres.models import SourceConnectionModel
from findr.adapters.outbound.postgres.oauth_state_repository_postgres import (
    OAuthStateRepositoryPostgres,
)
from findr.adapters.outbound.postgres.source_connection_repo_postgres import (
    SourceConnectionRepositoryPostgres,
)
from findr.adapters.outbound.postgres.user_repository_postgres import UserRepositoryPostgres
from findr.domain.value_objects import ConnectionStatus, SourceType
from conftest import ensure_workspace


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


def test_oauth_state_round_trip_is_one_time_use(db_session):
    user = UserRepositoryPostgres(db_session).create("a@example.com", "hash")
    db_session.commit()
    clock = FixedClock(datetime(2024, 1, 1))
    repo = OAuthStateRepositoryPostgres(db_session, clock=clock)

    state = repo.create(user_id=user.id, workspace_id=ensure_workspace(db_session, user.id).id, code_verifier="verifier-xyz", ttl_seconds=600)
    db_session.commit()

    result = repo.consume(state)
    db_session.commit()
    assert result is not None
    assert result.user_id == user.id
    assert result.code_verifier == "verifier-xyz"

    assert repo.consume(state) is None  # one-time use


def test_oauth_state_expired_returns_none(db_session):
    user = UserRepositoryPostgres(db_session).create("a@example.com", "hash")
    db_session.commit()
    clock = FixedClock(datetime(2024, 1, 1))
    repo = OAuthStateRepositoryPostgres(db_session, clock=clock)
    state = repo.create(user_id=user.id, workspace_id=ensure_workspace(db_session, user.id).id, code_verifier="v", ttl_seconds=60)
    db_session.commit()

    clock.current = datetime(2024, 1, 1, 0, 5)  # 5 minutes later, past the 60s ttl
    assert repo.consume(state) is None


def test_source_connection_create_get_and_status_lifecycle(db_session):
    user = UserRepositoryPostgres(db_session).create("a@example.com", "hash")
    db_session.commit()
    repo = SourceConnectionRepositoryPostgres(db_session)

    connection = repo.create(user.id, ensure_workspace(db_session, user.id).id, SourceType.GMAIL, "a@gmail.com")
    db_session.commit()

    assert repo.get(connection.id, user.id).external_account == "a@gmail.com"
    assert repo.get_by_account(user.id, SourceType.GMAIL, "a@gmail.com").id == connection.id
    assert repo.get(connection.id, user_id=999) is None  # not this user's connection

    repo.update_status(connection.id, ConnectionStatus.NEEDS_REAUTH, last_error="token expired")
    db_session.commit()
    updated = repo.get(connection.id, user.id)
    assert updated.status == ConnectionStatus.NEEDS_REAUTH
    assert updated.last_error == "token expired"
    assert repo.list_active() == []

    repo.update_status(connection.id, ConnectionStatus.ACTIVE)
    db_session.commit()
    active = repo.list_active()
    assert len(active) == 1
    assert active[0].last_error is None  # cleared on returning to ACTIVE


def test_source_connection_display_name_round_trip(db_session):
    user = UserRepositoryPostgres(db_session).create("a@example.com", "hash")
    db_session.commit()
    repo = SourceConnectionRepositoryPostgres(db_session)

    connection = repo.create(user.id, ensure_workspace(db_session, user.id).id, SourceType.GMAIL, "ada@acme.com", "Acme Corp (Ada)")
    db_session.commit()
    assert repo.get(connection.id, user.id).display_name == "Acme Corp (Ada)"

    repo.update_display_name(connection.id, "Acme Corp (Ada Lovelace)")
    db_session.commit()
    assert repo.get(connection.id, user.id).display_name == "Acme Corp (Ada Lovelace)"

    # update_status/update_cursor must not clobber the display name.
    repo.update_status(connection.id, ConnectionStatus.NEEDS_REAUTH)
    repo.update_cursor(connection.id, "cursor-1", datetime(2024, 1, 1))
    db_session.commit()
    assert repo.get(connection.id, user.id).display_name == "Acme Corp (Ada Lovelace)"


def test_credential_store_encrypts_tokens_at_rest(db_session):
    user = UserRepositoryPostgres(db_session).create("a@example.com", "hash")
    db_session.commit()
    connection = SourceConnectionRepositoryPostgres(db_session).create(
        user.id, ensure_workspace(db_session, user.id).id, SourceType.GMAIL, "a@gmail.com"
    )
    db_session.commit()

    cipher = TokenCipher(Fernet.generate_key().decode())
    store = CredentialStorePostgres(db_session, cipher)

    assert store.get(connection.id) is None

    store.save(connection.id, "access-token-value", "refresh-token-value", datetime(2030, 1, 1))
    db_session.commit()

    creds = store.get(connection.id)
    assert creds.access_token == "access-token-value"
    assert creds.refresh_token == "refresh-token-value"

    row = db_session.get(SourceConnectionModel, connection.id)
    assert b"access-token-value" not in row.access_token_enc

    store.delete(connection.id)
    db_session.commit()
    assert store.get(connection.id) is None
