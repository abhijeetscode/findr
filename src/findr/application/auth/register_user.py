from findr.domain.entities import User
from findr.domain.exceptions import DuplicateUser
from findr.ports.password_hasher import PasswordHasher
from findr.ports.user_repository import UserRepository


class RegisterUser:
    def __init__(self, user_repo: UserRepository, hasher: PasswordHasher) -> None:
        self._user_repo = user_repo
        self._hasher = hasher

    def execute(self, email: str, password: str) -> User:
        if self._user_repo.get_by_email(email) is not None:
            raise DuplicateUser(f"User with email {email!r} already exists")
        password_hash = self._hasher.hash(password)
        return self._user_repo.create(email, password_hash)
