"""Symmetric encryption for the secrets stored in the database (PVE/PBS API
tokens, enrollment secrets, restore-server credentials, SMTP password, Gotify
token).

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
    # No fallback to plaintext: a secret that cannot be encrypted is not stored.
    return _fernet().encrypt(plaintext.encode()).decode()


def is_encrypted(value: str) -> bool:
    """Whether `value` is a token of the configured key (False without a key)."""
    if not value or not SECRET_KEY:
        return False
    try:
        _fernet().decrypt(value.encode())
    except Exception:
        return False
    return True


def encrypt_once(value: str) -> str:
    """Encrypt unless `value` already is — for values read back and stored again."""
    return value if is_encrypted(value) else encrypt(value)


def keyed_digest(purpose: str, message: str) -> str:
    """HMAC-SHA256 under a key derived from BSENTINEL_SECRET_KEY: a digest that
    cannot be recomputed with database access alone."""
    import hashlib
    import hmac

    key = hashlib.sha256(f"bsentinel:{purpose}:{SECRET_KEY}".encode()).digest()
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


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
