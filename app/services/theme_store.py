"""The installation's active theme, read from app_settings."""

from __future__ import annotations

from app import db
from app.services import theme


def active_theme() -> tuple[theme.Preset, theme.BaseColors]:
    """Preset + the four base colours from the settings (invalid values ignored)."""
    stored = db.get_settings_dict("theme_")
    overrides = {key: stored.get(f"theme_{key}", "") for key in theme.BASE_KEYS}
    return theme.resolve(stored.get("theme_preset"), overrides)
