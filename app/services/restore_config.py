"""Load/save the restore-server (PVE-Restore) configuration.

Stored in ``app_settings`` under the ``restore_server.`` prefix: the non-secret
structure as one JSON blob, the two secrets (API token secret, SSH private key)
as separate Fernet-encrypted values (`app.services.crypto`). Secrets are never
returned to templates and never logged.
"""

from __future__ import annotations

import json
from typing import Any

from app.db_settings import get_settings_dict, save_settings
from app.models import RestoreServerPayload
from app.services.crypto import decrypt, encrypt

_CONFIG_KEY = "restore_server.config"
_TOKEN_KEY = "restore_server.token_secret"
_SSHKEY_KEY = "restore_server.ssh_private_key"
_SECRET_FIELDS = ("token_secret", "ssh_private_key")


def is_configured() -> bool:
    return bool(get_settings_dict(_CONFIG_KEY).get(_CONFIG_KEY))


# Fields the form may omit (sent empty) without clearing the stored value:
# the two secrets, and the SSH host key (managed automatically — pinned on first
# connect, cleared only via the explicit button).
_KEEP_IF_EMPTY = (*_SECRET_FIELDS, "ssh_host_key")


def _persist(cfg: dict[str, Any]) -> None:
    non_secret = {k: v for k, v in cfg.items() if k not in _SECRET_FIELDS}
    save_settings(
        {
            _CONFIG_KEY: json.dumps(non_secret),
            _TOKEN_KEY: encrypt(cfg.get("token_secret", "")),
            _SSHKEY_KEY: encrypt(cfg.get("ssh_private_key", "")),
        }
    )


def save(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate + persist. Returns the normalized config (with secrets). Empty
    secrets / host key in the payload keep the previously stored value (so the UI
    need not re-send them, and a form save never wipes the auto-pinned host key)."""
    cfg = RestoreServerPayload.model_validate(payload).model_dump()
    existing = load() or {}
    for field in _KEEP_IF_EMPTY:
        if not cfg.get(field):
            cfg[field] = existing.get(field, "")
    _persist(cfg)
    return cfg


def set_host_key(line: str) -> None:
    """Explicitly set (auto-pin) or clear (empty) the SSH host key, bypassing the
    keep-if-empty rule used by form saves."""
    cfg = load()
    if cfg is None:
        return
    cfg["ssh_host_key"] = line
    _persist(cfg)


def load() -> dict[str, Any] | None:
    """Return the full config (secrets decrypted) or None if not configured."""
    settings = get_settings_dict("restore_server.")
    raw = settings.get(_CONFIG_KEY)
    if not raw:
        return None
    cfg: dict[str, Any] = json.loads(raw)
    cfg["token_secret"] = decrypt(settings.get(_TOKEN_KEY, ""))
    cfg["ssh_private_key"] = decrypt(settings.get(_SSHKEY_KEY, ""))
    return cfg


_PAUSED_KEY = "restore_server.queue_paused"


def queue_paused() -> bool:
    return get_settings_dict(_PAUSED_KEY).get(_PAUSED_KEY) == "1"


def set_queue_paused(paused: bool) -> None:
    save_settings({_PAUSED_KEY: "1" if paused else "0"})


def public_config() -> dict[str, Any] | None:
    """Config for templates: secrets stripped, replaced by booleans."""
    cfg = load()
    if cfg is None:
        return None
    safe = {k: v for k, v in cfg.items() if k not in _SECRET_FIELDS}
    safe["has_token_secret"] = bool(cfg.get("token_secret"))
    safe["has_ssh_private_key"] = bool(cfg.get("ssh_private_key"))
    return safe
