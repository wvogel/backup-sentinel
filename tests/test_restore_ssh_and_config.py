"""Pure tests: SSH key parsing (multiple types), host-key pinning, and the
whitespace-stripping of restore-server credential fields."""

import io

import paramiko

from app.models import RestoreServerPayload
from app.services.restore_server import add_host_key, load_private_key


def _ed25519_openssh() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()


def _ed25519_pub_line(host: str = "10.0.0.9") -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pub = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    return f"{host} {pub}"


def _rsa_pem() -> str:
    key = paramiko.RSAKey.generate(2048)
    buf = io.StringIO()
    key.write_private_key(buf)
    return buf.getvalue()


def test_load_ed25519_key():
    pkey = load_private_key(_ed25519_openssh())
    assert isinstance(pkey, paramiko.Ed25519Key)


def test_load_rsa_key():
    pkey = load_private_key(_rsa_pem())
    assert isinstance(pkey, paramiko.RSAKey)


def test_load_invalid_key_raises():
    try:
        load_private_key("not a key")
    except ValueError as exc:
        assert "SSH private key" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_add_host_key_accepts_keyscan_line():
    client = paramiko.SSHClient()
    add_host_key(client, "10.0.0.9", _ed25519_pub_line("10.0.0.9"))
    assert client.get_host_keys().lookup("10.0.0.9") is not None


def _payload(**over):
    base = dict(
        name="R",
        api_url="https://restore:8006",
        api_user="root@pam",
        token_id="restore",
        ssh_host="10.0.0.9",
        mgmt_iface="vmbr0",
        mgmt_netmask="255.255.255.0",
        mgmt_ip_from="10.9.0.10",
        mgmt_ip_to="10.9.0.20",
    )
    base.update(over)
    return base


def test_strip_trims_token_secret_and_fields():
    # A pasted secret often carries a trailing newline → 401 without stripping.
    p = RestoreServerPayload.model_validate(_payload(token_secret="  secret-uuid\n", api_user=" root@pam "))
    assert p.token_secret == "secret-uuid"
    assert p.api_user == "root@pam"


def test_ssh_private_key_is_not_stripped():
    key = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"
    p = RestoreServerPayload.model_validate(_payload(ssh_private_key=key))
    assert p.ssh_private_key == key  # newlines preserved


def test_ensure_host_key_pinned_keeps_existing():
    from app.services import restore_orchestrator as orch

    cfg = {"ssh_host": "10.0.0.9", "ssh_host_key": "10.0.0.9 ssh-ed25519 AAAA"}
    assert orch.ensure_host_key_pinned(cfg) is cfg  # unchanged, no scan


def test_ensure_host_key_pinned_scan_failure_returns_unchanged():
    from app.services import restore_orchestrator as orch

    # Refused connection → scan fails fast → config returned unchanged (no raise).
    cfg = {"ssh_host": "127.0.0.1", "ssh_port": 1, "ssh_host_key": ""}
    out = orch.ensure_host_key_pinned(cfg)
    assert out.get("ssh_host_key", "") == ""
