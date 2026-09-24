from argon2 import PasswordHasher as Argon2PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


class Argon2Hasher:
    """Implements ports.password_hasher.PasswordHasher."""

    def __init__(self) -> None:
        self._hasher = Argon2PasswordHasher()

    def hash(self, plaintext: str) -> str:
        return self._hasher.hash(plaintext)

    def verify(self, plaintext: str, hashed: str) -> bool:
        try:
            return self._hasher.verify(hashed, plaintext)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
