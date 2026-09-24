from cryptography.fernet import Fernet

from findr.adapters.outbound.crypto.token_cipher import TokenCipher


def test_encrypt_decrypt_round_trip():
    cipher = TokenCipher(Fernet.generate_key().decode())

    ciphertext = cipher.encrypt("super-secret-token")

    assert ciphertext != b"super-secret-token"
    assert cipher.decrypt(ciphertext) == "super-secret-token"
