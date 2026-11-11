"""Carries a pasted sign-in secret from the dashboard to ONE worker process, sealed.

The worker makes a fresh RSA key per login and publishes only the public half. The dashboard
seals the pasted text to it: a one-time AES-256-GCM key encrypts the text, RSA-OAEP-SHA256
wraps that key (a Codex callback address is longer than plain RSA can carry). The private key
never leaves the worker's memory, so neither the database nor anyone reading it can recover
the secret. Workers hold no real TOKEN_SECRET on purpose (docker-compose.yml), which is why
Fernet is not used here.
"""
from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                     algorithm=hashes.SHA256(), label=None)
MAX_SECRET_BYTES = 4096


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_pem(key: rsa.RSAPrivateKey) -> str:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def seal(pem: str, secret: str) -> str:
    """ValueError when the text is empty or too long, or the key is not an RSA public key."""
    data = secret.strip().encode()
    if not data or len(data) > MAX_SECRET_BYTES:
        raise ValueError("the sign-in code is empty or too long")
    public = serialization.load_pem_public_key(pem.encode())
    if not isinstance(public, rsa.RSAPublicKey):
        raise ValueError("not an RSA public key")
    aes, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
    box = {"k": public.encrypt(aes, _OAEP), "n": nonce, "c": AESGCM(aes).encrypt(nonce, data, None)}
    return base64.b64encode(json.dumps(
        {k: base64.b64encode(v).decode() for k, v in box.items()}).encode()).decode()


def unseal(key: rsa.RSAPrivateKey, sealed: str) -> str:
    box = {k: base64.b64decode(v) for k, v in json.loads(base64.b64decode(sealed)).items()}
    aes = key.decrypt(box["k"], _OAEP)
    return AESGCM(aes).decrypt(box["n"], box["c"], None).decode()
