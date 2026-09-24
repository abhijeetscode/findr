from datetime import datetime

import pytest

from findr.application.auth.login_user import LoginUser
from findr.application.auth.register_user import RegisterUser
from findr.domain.entities import User
from findr.domain.exceptions import DuplicateUser, InvalidCredentials


class FakeUserRepository:
    """In-memory stand-in for ports.user_repository.UserRepository."""

    def __init__(self) -> None:
        self._users: dict[str, User] = {}
        self._next_id = 1

    def get_by_email(self, email: str) -> User | None:
        return self._users.get(email)

    def get_by_id(self, user_id: int) -> User | None:
        return next((u for u in self._users.values() if u.id == user_id), None)

    def create(self, email: str, password_hash: str) -> User:
        user = User(
            id=self._next_id,
            email=email,
            password_hash=password_hash,
            created_at=datetime(2024, 1, 1),
        )
        self._users[email] = user
        self._next_id += 1
        return user


class FakePasswordHasher:
    """Deterministic stand-in for ports.password_hasher.PasswordHasher."""

    def hash(self, plaintext: str) -> str:
        return f"hashed:{plaintext}"

    def verify(self, plaintext: str, hashed: str) -> bool:
        return hashed == f"hashed:{plaintext}"


class FakeSessionStore:
    """In-memory stand-in for ports.session_store.SessionStore."""

    def __init__(self) -> None:
        self._sessions: dict[str, int] = {}
        self._counter = 0

    def create(self, user_id: int) -> str:
        self._counter += 1
        token = f"token-{self._counter}"
        self._sessions[token] = user_id
        return token

    def get_user_id(self, token: str) -> int | None:
        return self._sessions.get(token)

    def delete(self, token: str) -> None:
        self._sessions.pop(token, None)


def test_register_user_creates_account_with_hashed_password():
    repo = FakeUserRepository()
    use_case = RegisterUser(repo, FakePasswordHasher())

    user = use_case.execute("a@example.com", "secret123")

    assert user.email == "a@example.com"
    assert user.password_hash == "hashed:secret123"


def test_register_user_rejects_duplicate_email():
    repo = FakeUserRepository()
    use_case = RegisterUser(repo, FakePasswordHasher())
    use_case.execute("a@example.com", "secret123")

    with pytest.raises(DuplicateUser):
        use_case.execute("a@example.com", "different-password")


def test_login_user_returns_a_usable_session_token_on_success():
    repo = FakeUserRepository()
    hasher = FakePasswordHasher()
    RegisterUser(repo, hasher).execute("a@example.com", "secret123")
    session_store = FakeSessionStore()
    use_case = LoginUser(repo, hasher, session_store)

    token = use_case.execute("a@example.com", "secret123")

    assert session_store.get_user_id(token) == repo.get_by_email("a@example.com").id


def test_login_user_rejects_wrong_password():
    repo = FakeUserRepository()
    hasher = FakePasswordHasher()
    RegisterUser(repo, hasher).execute("a@example.com", "secret123")
    use_case = LoginUser(repo, hasher, FakeSessionStore())

    with pytest.raises(InvalidCredentials):
        use_case.execute("a@example.com", "wrong-password")


def test_login_user_rejects_unknown_email():
    use_case = LoginUser(FakeUserRepository(), FakePasswordHasher(), FakeSessionStore())

    with pytest.raises(InvalidCredentials):
        use_case.execute("nobody@example.com", "whatever")
