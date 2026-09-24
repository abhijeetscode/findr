from findr.domain.exceptions import InvalidCredentials
from findr.ports.password_hasher import PasswordHasher
from findr.ports.session_store import SessionStore
from findr.ports.user_repository import UserRepository


class LoginUser:
    def __init__(
        self,
        user_repo: UserRepository,
        hasher: PasswordHasher,
        session_store: SessionStore,
    ) -> None:
        self._user_repo = user_repo
        self._hasher = hasher
        self._session_store = session_store

    def execute(self, email: str, password: str) -> str:
        user = self._user_repo.get_by_email(email)
        if user is None or not self._hasher.verify(password, user.password_hash):
            raise InvalidCredentials("Invalid email or password")
        return self._session_store.create(user.id)
