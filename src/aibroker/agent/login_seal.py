"""Carries the Claude sign-in code from the dashboard to ONE worker process, sealed.

The worker makes a fresh key pair per login and publishes only the public half; the dashboard
encrypts the pasted code to it (RSA-OAEP-SHA256). The private key never leaves the worker's
memory, so neither the database nor anyone reading it can recover the code. Workers hold no
real TOKEN_SECRET on purpose (docker-compose.yml), which is why Fernet is not used here.
"""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                     algorithm=hashes.SHA256(), label=None)
MAX_CODE_CHARS = 400   # an OAuth code is far shorter; RSA-2048 OAEP-SHA256 fits 190 bytes


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_pem(key: rsa.RSAPrivateKey) -> str:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def seal(pem: str, code: str) -> str:
    """ValueError when the code is empty or too long, or the key is not an RSA public key."""
    code = code.strip()
    if not code or len(code.encode()) > 190:
        raise ValueError("the sign-in code is empty or too long")
    public = serialization.load_pem_public_key(pem.encode())
    if not isinstance(public, rsa.RSAPublicKey):
        raise ValueError("not an RSA public key")
    return base64.b64encode(public.encrypt(code.encode(), _OAEP)).decode()


def unseal(key: rsa.RSAPrivateKey, sealed: str) -> str:
    return key.decrypt(base64.b64decode(sealed), _OAEP).decode()
