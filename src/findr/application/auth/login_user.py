import logging

from findr.domain.exceptions import InvalidCredentials
from findr.observability import log_event
from findr.ports.password_hasher import PasswordHasher
from findr.ports.session_store import SessionStore
from findr.ports.user_repository import UserRepository

logger = logging.getLogger(__name__)


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
            # No username: it's what was typed, not necessarily an account.
            log_event(
                logger, "auth.login.failed", level=logging.WARNING, reason="bad_credentials"
            )
            raise InvalidCredentials("Invalid email or password")
        token = self._session_store.create(user.id)
        log_event(logger, "auth.login.succeeded", user_id=user.id)
        return token
