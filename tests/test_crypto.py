"""Fernet roundtrip."""
from aibroker.crypto import decrypt, encrypt


def test_roundtrip():
    plain = "csk-abc123-XYZ"
    enc = encrypt(plain)
    assert enc != plain
    assert decrypt(enc) == plain


def test_different_ciphertexts():
    # Fernet adds random IV → same plaintext → different ciphertext
    plain = "secret"
    assert encrypt(plain) != encrypt(plain)
