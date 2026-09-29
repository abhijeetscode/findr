from datetime import datetime

from findr.adapters.outbound.postgres.session_store_postgres import SessionStorePostgres
from findr.adapters.outbound.postgres.user_repository_postgres import UserRepositoryPostgres


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


def test_user_repository_create_and_lookup(db_session):
    repo = UserRepositoryPostgres(db_session)

    created = repo.create("a@example.com", "hashed-pw")
    db_session.commit()

    assert created.id is not None
    assert repo.get_by_email("a@example.com").email == "a@example.com"
    assert repo.get_by_id(created.id).email == "a@example.com"
    assert repo.get_by_email("missing@example.com") is None


def test_session_store_create_lookup_expire_and_delete(db_session):
    clock = FixedClock(datetime(2024, 1, 1, 12, 0, 0))
    user = UserRepositoryPostgres(db_session, clock=clock).create("a@example.com", "hashed-pw")
    db_session.commit()
    store = SessionStorePostgres(db_session, clock=clock, ttl_days=1)

    token = store.create(user_id=user.id)
    db_session.commit()
    assert store.get_user_id(token) == user.id

    clock.current = datetime(2024, 1, 3, 12, 0, 0)  # past the 1-day TTL
    assert store.get_user_id(token) is None

    clock.current = datetime(2024, 1, 1, 12, 0, 0)
    token2 = store.create(user_id=user.id)
    db_session.commit()
    store.delete(token2)
    db_session.commit()
    assert store.get_user_id(token2) is None
