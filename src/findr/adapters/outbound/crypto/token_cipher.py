from cryptography.fernet import Fernet


class TokenCipher:
    """Column-level encryption for OAuth tokens at rest (Fernet/AES).

    Not full-disk/DB encryption — see specs/gmail-connector.md's out-of-scope
    note. `key` must be a 32-byte urlsafe-base64 Fernet key; generate one
    with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

    Validates the key lazily (on first encrypt/decrypt), not at construction
    — a router builds one of these per request regardless of whether the
    request path ends up touching a token (e.g. disconnecting a connection
    that turns out not to exist), so an unset/placeholder
    FINDR_TOKEN_ENCRYPTION_KEY shouldn't break requests that never need it.
    """

    def __init__(self, key: str) -> None:
        self._key = key
        self._fernet: Fernet | None = None

    def _get_fernet(self) -> Fernet:
        if self._fernet is None:
            self._fernet = Fernet(self._key.encode("ascii"))
        return self._fernet

    def encrypt(self, plaintext: str) -> bytes:
        return self._get_fernet().encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        return self._get_fernet().decrypt(ciphertext).decode("utf-8")
