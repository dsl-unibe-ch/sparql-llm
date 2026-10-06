"""Symmetric encryption helpers for storing API keys at rest.

Uses Fernet (AES-128-CBC + HMAC-SHA256) with a key derived deterministically
from ``AUTH_SECRET``, so every process instance encrypts and decrypts with the
same key without an extra secret to manage.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


def _derive_key(secret: str) -> bytes:
    """Derive a 32-byte URL-safe-base64-encoded Fernet key from *secret*."""
    raw = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(raw)


def encrypt_api_key(plain: str, secret: str) -> str:
    """Encrypt *plain* text and return the Fernet token as a UTF-8 string."""
    return Fernet(_derive_key(secret)).encrypt(plain.encode()).decode()


def decrypt_api_key(token: str, secret: str) -> str:
    """Decrypt a Fernet *token* and return the original plaintext.

    Raises ``InvalidToken`` if the token was tampered with or the wrong
    secret is used.
    """
    return Fernet(_derive_key(secret)).decrypt(token.encode()).decode()


def mask_key(key: str) -> str:
    """Return a masked version of an API key for display purposes.

    Shows the first 4 and last 4 characters (e.g. ``sk-p…****k29H``).
    Short keys (≤10 chars) are fully masked.
    """
    if len(key) <= 10:
        return "****"
    return f"{key[:4]}…****{key[-4:]}"
