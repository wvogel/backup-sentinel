"""Small hardenings: what is sent where, and what an unauthenticated caller learns."""

import json
from unittest import mock

import pytest
from pydantic import ValidationError

from app.models import BootstrapFinalize
from app.services import notifications


def _gotify(url: str):
    """Send one message with the given Gotify URL; returns (sent, request or None)."""
    seen = []

    def urlopen(req, timeout=None):
        seen.append(req)
        return mock.MagicMock()

    with (
        mock.patch.object(notifications.db, "get_settings_dict", return_value={"gotify_url": url, "gotify_token": "x"}),
        mock.patch.object(notifications, "decrypt", return_value="s3cret-token"),
        mock.patch.object(notifications.request, "urlopen", urlopen),
    ):
        sent = notifications._send_gotify_raw("title", "message", 5)
    return sent, (seen[0] if seen else None)


def test_gotify_token_travels_as_a_header_not_in_the_url():
    sent, req = _gotify("https://gotify.example")
    assert sent
    assert req.full_url == "https://gotify.example/message"
    assert "s3cret-token" not in req.full_url
    assert req.get_header("X-gotify-key") == "s3cret-token"
    assert json.loads(req.data)["title"] == "title"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://gotify.example", "gotify.example"])
def test_gotify_url_must_be_http(url):
    sent, req = _gotify(url)
    assert not sent and req is None


def test_unknown_mail_encryption_sends_nothing():
    """No silent fall-back to an unencrypted login."""
    cfg = {
        "email_host": "mail.example",
        "email_recipients": "ops@example.com",
        "email_from": "sentinel@example.com",
        "email_user": "u",
        "email_password": "p",
        "email_tls": "tls",
    }
    with (
        mock.patch.object(notifications, "_email_config", return_value=cfg),
        mock.patch.object(notifications, "decrypt", return_value="p"),
        mock.patch.object(notifications.smtplib, "SMTP") as plain,
        mock.patch.object(notifications.smtplib, "SMTP_SSL") as ssl_,
    ):
        assert notifications._send_email_raw("subject", "body") is False
    assert not plain.called and not ssl_.called


def test_healthz_does_not_tell_why_the_database_is_down():
    from fastapi.testclient import TestClient

    from app import main

    with mock.patch.object(main.db, "connect", side_effect=RuntimeError("password for user backup_reports@db-host")):
        response = TestClient(main.app).get("/healthz")
    assert response.status_code == 503
    assert response.json()["database"] == "unreachable"
    assert "backup_reports" not in response.text


def test_bootstrap_finalize_fields_are_bounded():
    ok = {
        "cluster_slug": "main",
        "enrollment_secret": "s" * 32,
        "proxmox_node": "pve1",
        "api_user": "backup-reports@pve",
        "token_id": "collector",
        "token_secret": "t" * 36,
    }
    BootstrapFinalize(**ok)
    for field, value in (("token_secret", "t" * 401), ("cluster_slug", "c" * 81), ("fingerprint", "f" * 201)):
        with pytest.raises(ValidationError):
            BootstrapFinalize(**{**ok, field: value})
