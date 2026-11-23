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

from app.config import SECRET_KEY, SECRET_KEYS_OLD

logger = logging.getLogger(__name__)

# Every Fernet token starts like this (version byte 0x80 + the high bytes of
# its timestamp). It tells an encrypted value from a plaintext one WITHOUT a
# key — so a value encrypted with another key is never mistaken for plaintext.
_TOKEN_PREFIX = "gAAAAA"
_MAX_LAYERS = 8


class UndecryptableError(ValueError):
    """A stored secret is encrypted with a key this installation does not have."""


def looks_encrypted(value: str) -> bool:
    return bool(value) and value.startswith(_TOKEN_PREFIX)


def _fernets() -> list:
    """The current key first, then the previous ones (BSENTINEL_SECRET_KEY_OLD)."""
    from cryptography.fernet import Fernet

    fernets = []
    for key in (SECRET_KEY, *SECRET_KEYS_OLD):
        try:
            fernets.append(Fernet(key.encode()))
        except Exception:
            logger.error("A key in BSENTINEL_SECRET_KEY_OLD is not a valid Fernet key and is ignored")
    return fernets


def _open_layer(token: str) -> tuple[str, int] | None:
    """Decrypt one layer with whichever key fits: (content, index of the key), or None."""
    for index, fernet in enumerate(_fernets()):
        try:
            return fernet.decrypt(token.encode()).decode(), index
        except Exception:
            continue
    return None


def unwrap(value: str) -> tuple[str, bool]:
    """The plaintext of a stored value and whether it is stored as it should be
    (one layer, current key). Opens every layer with any known key — a value
    wrapped more than once by a start with a changed key (v3.20.0) comes out
    whole. Raises UndecryptableError when a layer fits no known key."""
    if not value or not SECRET_KEY:
        return value, True
    if not looks_encrypted(value):
        return value, False  # plaintext from before encryption
    current, layers, only_current_key = value, 0, True
    while looks_encrypted(current):
        if layers >= _MAX_LAYERS:
            raise UndecryptableError("too many layers of encryption")
        opened = _open_layer(current)
        if opened is None:
            raise UndecryptableError("encrypted with a key that is not configured")
        current, key_index = opened
        only_current_key = only_current_key and key_index == 0
        layers += 1
    return current, layers == 1 and only_current_key


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns plaintext unchanged if no key is configured."""
    if not plaintext or not SECRET_KEY:
        return plaintext
    # No fallback to plaintext: a secret that cannot be encrypted is not stored.
    return _fernets()[0].encrypt(plaintext.encode()).decode()


def is_encrypted(value: str) -> bool:
    """Whether `value` is a token of the current key (False without a key)."""
    if not value or not SECRET_KEY:
        return False
    try:
        _fernets()[0].decrypt(value.encode())
    except Exception:
        return False
    return True


def keyed_digests(purpose: str, message: str) -> list[str]:
    """HMAC-SHA256 under a key derived from BSENTINEL_SECRET_KEY — a digest that
    cannot be recomputed with database access alone. First for the current key,
    then for each previous one (to verify what was written before a key change)."""
    import hashlib
    import hmac

    digests = []
    for secret in (SECRET_KEY, *SECRET_KEYS_OLD):
        key = hashlib.sha256(f"bsentinel:{purpose}:{secret}".encode()).digest()
        digests.append(hmac.new(key, message.encode(), hashlib.sha256).hexdigest())
    return digests


def keyed_digest(purpose: str, message: str) -> str:
    return keyed_digests(purpose, message)[0]


def decrypt(ciphertext: str) -> str:
    """Decrypt a stored value. A plaintext value from before encryption is
    returned as it is. A value encrypted with an unknown key yields "" — never
    the ciphertext, which callers would otherwise send out as a password."""
    try:
        return unwrap(ciphertext)[0]
    except UndecryptableError as exc:
        logger.error("A stored secret cannot be decrypted (%s): check BSENTINEL_SECRET_KEY", exc)
        return ""
