"""TLS trust for PVE/PBS: CA-verified or pinned, never unchecked.

Runs a real HTTPS server with a self-signed certificate; the pin store is a
dict in place of the database.
"""

import datetime
import http.server
import ipaddress
import ssl
import threading
from typing import ClassVar

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.services import api_client, tls_trust
from app.services.api_client import APIError, api_get


def _self_signed(tmp_path, name: str):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pve.example")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_file, key_file, tls_trust.fingerprint(cert.public_bytes(serialization.Encoding.DER))


class _Handler(http.server.BaseHTTPRequestHandler):
    requests_seen: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        _Handler.requests_seen.append(self.headers.get("Authorization", ""))
        body = b'{"data": {"ok": true}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class _TLSServer(http.server.ThreadingHTTPServer):
    """Wraps each accepted connection with the context current at that moment."""

    context: ssl.SSLContext

    def get_request(self):
        sock, addr = super().get_request()
        return self.context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr


class _Server:
    """HTTPS server on 127.0.0.1 whose certificate can be swapped while it runs."""

    def __init__(self, cert_file, key_file) -> None:
        self.httpd = _TLSServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        self.use(cert_file, key_file)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def use(self, cert_file, key_file) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_file, key_file)
        self.httpd.context = context

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def store(monkeypatch):
    """Pins and reported changes, in memory; resets what the process remembers."""
    state = {"pins": {}, "changes": [], "bootstrap": set()}

    def save_pin(host, port, presented):
        state["pins"][(host, port)] = (presented, "bootstrap" if presented in state["bootstrap"] else "first-use")

    monkeypatch.setattr(tls_trust, "_load_pin", lambda host, port: (state["pins"].get((host, port)) or (None,))[0])
    monkeypatch.setattr(tls_trust, "_save_pin", save_pin)
    monkeypatch.setattr(
        tls_trust, "_save_ca", lambda host, port, fp: state["pins"].__setitem__((host, port), (fp, "ca"))
    )
    monkeypatch.setattr(tls_trust, "_save_change", lambda host, port, fp: state["changes"].append((host, port, fp)))
    monkeypatch.setattr(tls_trust, "_ca_seen", {})
    monkeypatch.setattr(tls_trust, "_pins", {})
    monkeypatch.setattr(tls_trust, "_reported", set())
    monkeypatch.setattr(tls_trust, "_not_ca_valid", set())
    monkeypatch.setattr(api_client, "_local", threading.local())  # no pooled connection from another test
    _Handler.requests_seen = []
    return state


@pytest.fixture
def server(tmp_path):
    first = _self_signed(tmp_path, "first")
    second = _self_signed(tmp_path, "second")
    srv = _Server(first[0], first[1])
    srv.first, srv.second = first, second
    yield srv
    srv.close()


def _get(server):
    return api_get(server.url, "/api2/json/version", "PVEAPIToken=u!t=secret", retries=0)


def _fresh_connection():
    api_client._local.__dict__.pop("pool", None)


def test_decide():
    assert tls_trust.decide(None, "AA", True) == "pin"
    assert tls_trust.decide("AA", "aa", True) == "ok"
    assert tls_trust.decide("AA", "BB", True) == "refuse"
    assert tls_trust.decide("AA", "BB", False) == "report"


def test_self_signed_certificate_is_pinned_on_first_contact(store, server, monkeypatch):
    monkeypatch.setattr(tls_trust, "ENFORCE", True)
    assert _get(server) == {"ok": True}
    assert store["pins"][("127.0.0.1", server.port)] == (server.first[2], "first-use")
    _fresh_connection()
    assert _get(server) == {"ok": True}  # the pinned certificate keeps working
    assert store["changes"] == []


def test_pin_matching_the_bootstrap_fingerprint_is_marked(store, server, monkeypatch):
    monkeypatch.setattr(tls_trust, "ENFORCE", True)
    store["bootstrap"].add(server.first[2])
    _get(server)
    assert store["pins"][("127.0.0.1", server.port)][1] == "bootstrap"


def test_changed_certificate_is_refused_before_the_token_is_sent(store, server, monkeypatch):
    monkeypatch.setattr(tls_trust, "ENFORCE", True)
    _get(server)
    sent_before = len(_Handler.requests_seen)
    server.use(server.second[0], server.second[1])  # someone else answers now
    _fresh_connection()
    with pytest.raises(APIError) as exc:
        _get(server)
    assert "certificate of 127.0.0.1" in str(exc.value) and server.second[2] in str(exc.value)
    assert len(_Handler.requests_seen) == sent_before  # no request, so no token, reached it
    assert store["changes"] == [("127.0.0.1", server.port, server.second[2])]
    assert store["pins"][("127.0.0.1", server.port)][0] == server.first[2]  # the pin stays
    _fresh_connection()
    with pytest.raises(APIError):
        _get(server)
    assert len(store["changes"]) == 1  # recorded once, not on every attempt


def test_accepting_the_new_certificate_makes_it_work_again(store, server, monkeypatch):
    monkeypatch.setattr(tls_trust, "ENFORCE", True)
    _get(server)
    server.use(server.second[0], server.second[1])
    _fresh_connection()
    with pytest.raises(APIError):
        _get(server)
    # What "accept" in the settings does: the pin becomes the certificate seen last.
    store["pins"][("127.0.0.1", server.port)] = (server.second[2], "accepted")
    tls_trust.forget("127.0.0.1", server.port)
    _fresh_connection()
    assert _get(server) == {"ok": True}


def test_report_only_mode_lets_the_change_through_but_records_it(store, server, monkeypatch):
    monkeypatch.setattr(tls_trust, "ENFORCE", False)  # BSENTINEL_INSECURE_SSL=true
    _get(server)
    server.use(server.second[0], server.second[1])
    _fresh_connection()
    assert _get(server) == {"ok": True}
    assert store["changes"] == [("127.0.0.1", server.port, server.second[2])]
    assert store["pins"][("127.0.0.1", server.port)][0] == server.first[2]  # not re-pinned by itself


def _ca_signed(tmp_path):
    """A CA and a certificate for 127.0.0.1 it vouches for: (ca file, cert file, key file, fingerprint)."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pve.example")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_file, cert_file, key_file = tmp_path / "ca.crt", tmp_path / "signed.crt", tmp_path / "signed.key"
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return ca_file, cert_file, key_file, tls_trust.fingerprint(cert.public_bytes(serialization.Encoding.DER))


@pytest.fixture
def ca_server(tmp_path, server, monkeypatch):
    """The server presents a certificate the app's CA store vouches for."""
    ca_file, cert_file, key_file, fp = _ca_signed(tmp_path)
    monkeypatch.setattr(tls_trust, "_verifying", ssl.create_default_context(cafile=str(ca_file)))
    monkeypatch.setattr(tls_trust, "_unverified", ssl._create_unverified_context())
    server.use(cert_file, key_file)
    server.signed = (cert_file, key_file, fp)
    return server


def test_ca_verified_host_is_remembered(store, ca_server, monkeypatch):
    monkeypatch.setattr(tls_trust, "ENFORCE", True)
    assert _get(ca_server) == {"ok": True}
    assert store["pins"][("127.0.0.1", ca_server.port)] == (ca_server.signed[2], "ca")
    assert store["changes"] == []


def test_ca_verified_host_is_not_downgraded_to_first_contact(store, ca_server, monkeypatch):
    """Someone in the path presents a self-signed certificate for a host a CA
    vouched for: refused like any changed certificate, never pinned afresh."""
    monkeypatch.setattr(tls_trust, "ENFORCE", True)
    _get(ca_server)
    sent_before = len(_Handler.requests_seen)
    ca_server.use(ca_server.first[0], ca_server.first[1])
    _fresh_connection()
    with pytest.raises(APIError) as exc:
        _get(ca_server)
    assert "certificate of 127.0.0.1" in str(exc.value)
    assert len(_Handler.requests_seen) == sent_before  # the token did not go out
    assert store["pins"][("127.0.0.1", ca_server.port)] == (ca_server.signed[2], "ca")
    assert store["changes"] == [("127.0.0.1", ca_server.port, ca_server.first[2])]


def test_ca_verified_host_stays_protected_after_a_restart(store, ca_server, monkeypatch):
    monkeypatch.setattr(tls_trust, "ENFORCE", True)
    _get(ca_server)
    for name in ("_pins", "_ca_seen"):  # a new process knows only what the database holds
        monkeypatch.setattr(tls_trust, name, {})
    monkeypatch.setattr(tls_trust, "_not_ca_valid", set())
    ca_server.use(ca_server.first[0], ca_server.first[1])
    _fresh_connection()
    with pytest.raises(APIError):
        _get(ca_server)


def test_renewed_ca_certificate_just_works(store, ca_server, tmp_path, monkeypatch):
    monkeypatch.setattr(tls_trust, "ENFORCE", True)
    _get(ca_server)
    renewed = tmp_path / "renewed"
    renewed.mkdir()
    ca_file, cert_file, key_file, fp = _ca_signed(renewed)
    monkeypatch.setattr(tls_trust, "_verifying", ssl.create_default_context(cafile=str(ca_file)))
    ca_server.use(cert_file, key_file)
    _fresh_connection()
    assert _get(ca_server) == {"ok": True}
    assert store["pins"][("127.0.0.1", ca_server.port)] == (fp, "ca")
    assert store["changes"] == []


def test_self_signed_host_that_gets_a_ca_certificate_is_upgraded(store, server, tmp_path, monkeypatch):
    monkeypatch.setattr(tls_trust, "ENFORCE", True)
    _get(server)  # pinned on first contact
    ca_file, cert_file, key_file, fp = _ca_signed(tmp_path)
    monkeypatch.setattr(tls_trust, "_verifying", ssl.create_default_context(cafile=str(ca_file)))
    monkeypatch.setattr(tls_trust, "_unverified", ssl._create_unverified_context())
    server.use(cert_file, key_file)
    _fresh_connection()
    with pytest.raises(APIError):
        _get(server)  # first seen as a change (the process skipped the CA check for this host) …
    _fresh_connection()
    assert _get(server) == {"ok": True}  # … then the CA check is tried again and vouches for it
    assert store["pins"][("127.0.0.1", server.port)] == (fp, "ca")


def test_fingerprint_format():
    assert tls_trust.fingerprint(b"x").count(":") == 31
    assert tls_trust.fingerprint(b"x") == tls_trust.fingerprint(b"x").upper()
