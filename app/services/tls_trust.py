"""Whom the app trusts on TLS when talking to PVE and PBS.

Proxmox hosts usually serve a self-signed certificate, which no public CA
vouches for. Instead of switching verification off, the app pins such a
certificate like SSH pins a host key:

* A certificate a system CA vouches for (right host name, not expired) is
  trusted as it is — nothing is pinned.
* Otherwise the SHA-256 fingerprint of the certificate is remembered on first
  contact, per host and port. If the host installed the fingerprint the
  bootstrap script reported, the pin is marked as coming from there.
* From then on only that certificate is accepted. A different one is recorded
  and refused — before any API token is sent — until an admin accepts it in
  the settings.

``BSENTINEL_INSECURE_SSL=true`` no longer means "do not look": it turns the
refusal into a report (the connection goes ahead, the change is recorded), as
a way to switch over without risk. Remove it to enforce the pins.
"""

from __future__ import annotations

import hashlib
import http.client
import logging
import ssl
import threading
from typing import Any

from app.config import INSECURE_SSL

logger = logging.getLogger(__name__)

ENFORCE = not INSECURE_SSL

_lock = threading.Lock()
_pins: dict[tuple[str, int], str] = {}  # what the database holds, read once per host
_reported: set[tuple[str, int, str]] = set()  # changes already recorded by this process
_not_ca_valid: set[tuple[str, int]] = set()  # hosts whose certificate no CA vouches for


class CertificateChanged(ssl.SSLError):
    """The host serves another certificate than the pinned one."""


def fingerprint(der: bytes) -> str:
    """SHA-256 as Proxmox shows it: AA:BB:…"""
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


def normalize(value: str | None) -> str:
    return (value or "").strip().upper()


def decide(pinned: str | None, presented: str, enforce: bool) -> str:
    """What to do with a certificate no CA vouches for:
    "pin" (first contact), "ok", "refuse" or "report" (changed)."""
    if not pinned:
        return "pin"
    if normalize(pinned) == normalize(presented):
        return "ok"
    return "refuse" if enforce else "report"


# ── storage (the database; swapped out in tests) ─────────────────────────────


def _load_pin(host: str, port: int) -> str | None:
    from app import db

    return db.get_tls_pin(host, port)


def _save_pin(host: str, port: int, presented: str) -> None:
    from app import db

    source = "bootstrap" if presented in db.bootstrap_fingerprints() else "first-use"
    db.save_tls_pin(host, port, presented, source)
    logger.info("TLS: pinned the certificate of %s:%s (%s, %s)", host, port, presented, source)
    _audit("tls.pinned", host, port, f"{presented} ({source})")


def _save_change(host: str, port: int, presented: str) -> None:
    from app import db

    db.save_tls_change(host, port, presented)
    _audit("tls.changed", host, port, presented)


def _audit(event: str, host: str, port: int, details: str) -> None:
    from app import db

    try:
        db.append_audit_event("system", event, f"{host}:{port}", details)
    except Exception as exc:
        logger.error("Audit entry %s for %s:%s failed: %s", event, host, port, exc)


def forget(host: str, port: int) -> None:
    """Drop what this process remembers about a host (after a change in the settings)."""
    with _lock:
        _pins.pop((host, port), None)
        for entry in [e for e in _reported if e[:2] == (host, port)]:
            _reported.discard(entry)


# ── the check ────────────────────────────────────────────────────────────────


def check(host: str, port: int, der: bytes, enforce: bool | None = None) -> None:
    """Judge the certificate of a host no CA vouches for. Raises
    CertificateChanged when it is not the pinned one and pins are enforced."""
    enforce = ENFORCE if enforce is None else enforce
    presented = fingerprint(der)
    key = (host, port)
    with _lock:
        if key not in _pins:
            pinned = _load_pin(host, port)
            if pinned:
                _pins[key] = pinned
        verdict = decide(_pins.get(key), presented, enforce)
        if verdict == "pin":
            _save_pin(host, port, presented)
            _pins[key] = presented
            return
        if verdict == "ok":
            return
        if (host, port, presented) not in _reported:
            _reported.add((host, port, presented))
            _save_change(host, port, presented)
            logger.error(
                "TLS: %s:%s serves another certificate (%s) than the pinned one (%s)%s",
                host,
                port,
                presented,
                _pins.get(key),
                "" if enforce else " — not enforced (BSENTINEL_INSECURE_SSL)",
            )
    if verdict == "refuse":
        raise CertificateChanged(
            f"certificate of {host}:{port} changed: it now has the fingerprint {presented}. "
            "If the certificate was renewed, accept it under Settings → Certificates."
        )


_verifying: ssl.SSLContext | None = None
_unverified: ssl.SSLContext | None = None


def _contexts() -> tuple[ssl.SSLContext, ssl.SSLContext]:
    global _verifying, _unverified
    if _verifying is None:
        _verifying = ssl.create_default_context()
        # Handshake without CA check — the fingerprint is checked right after it
        # (check()), before the connection is used for anything.
        _unverified = ssl._create_unverified_context()
    return _verifying, _unverified  # type: ignore[return-value]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a PVE/PBS: CA-verified if a CA vouches for the host, else pinned."""

    def connect(self) -> None:
        verifying, unverified = _contexts()
        key = (self.host, self.port)
        if key not in _not_ca_valid:
            self._context = verifying
            try:
                super().connect()
                return
            except ssl.SSLCertVerificationError:
                self._drop_socket()
                _not_ca_valid.add(key)
        self._context = unverified
        super().connect()
        der: Any = self.sock.getpeercert(binary_form=True) if self.sock else None
        try:
            if not der:
                raise CertificateChanged(f"{self.host}:{self.port} presented no certificate")
            check(self.host, self.port, der)
        except CertificateChanged:
            self._drop_socket()
            # The host may have got a CA-signed certificate meanwhile: try that next time.
            _not_ca_valid.discard(key)
            raise

    def _drop_socket(self) -> None:
        """Close the socket only: close() would also reset the request this
        connect() is part of (http.client connects while sending it)."""
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
