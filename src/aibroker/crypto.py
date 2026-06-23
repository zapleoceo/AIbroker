"""Fernet at-rest encryption for api_keys.token_encrypted.

The TOKEN_SECRET env var must be a urlsafe-base64 32-byte key. Generate:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet

from aibroker.config import get_settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(get_settings().TOKEN_SECRET.encode())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()
