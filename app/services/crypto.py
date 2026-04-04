"""Symmetric encryption for sensitive settings (SMTP password, Gotify token).

Requires BSENTINEL_SECRET_KEY to be set to a valid Fernet key.
Generate one with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Without BSENTINEL_SECRET_KEY, values are stored and returned as-is (no encryption).
When a key is set, existing unencrypted values are transparently returned as-is
on decrypt (migration path), and all new saves are encrypted.
"""
from __future__ import annotations

import logging

from app.config import SECRET_KEY

logger = logging.getLogger(__name__)


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(SECRET_KEY.encode())


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns plaintext unchanged if no key is configured."""
    if not plaintext or not SECRET_KEY:
        return plaintext
    try:
        return _fernet().encrypt(plaintext.encode()).decode()
    except Exception as exc:
        logger.warning("Verschlüsselung fehlgeschlagen: %s", exc)
        return plaintext


def decrypt(ciphertext: str) -> str:
    """Decrypt a string. Falls back to returning the value as-is if decryption
    fails (e.g. value was stored before encryption was enabled)."""
    if not ciphertext or not SECRET_KEY:
        return ciphertext
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except Exception:
        # Not (yet) encrypted — return as-is for seamless migration
        return ciphertext
