"""Pinned TLS certificates of the PVE/PBS hosts (see app.services.tls_trust)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.db_core import connect


def get_tls_pin(host: str, port: int) -> str | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT fingerprint FROM tls_pins WHERE host = %s AND port = %s", (host, port))
        row = cur.fetchone()
        return row["fingerprint"] if row else None


def tls_pin_source(host: str, port: int) -> str | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT source FROM tls_pins WHERE host = %s AND port = %s", (host, port))
        row = cur.fetchone()
        return row["source"] if row else None


def save_tls_pin(host: str, port: int, fingerprint: str, source: str) -> None:
    """Pin a certificate. `source`: "bootstrap" (matches what the bootstrap script
    reported), "first-use", "accepted" (by an admin, after a change) or "ca" (a
    system CA vouched for the host; the fingerprint is the certificate seen then)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tls_pins (host, port, fingerprint, source, pinned_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (host, port) DO UPDATE SET
                fingerprint = EXCLUDED.fingerprint, source = EXCLUDED.source, pinned_at = EXCLUDED.pinned_at,
                seen_fingerprint = NULL, seen_at = NULL
            """,
            (host, port, fingerprint, source, datetime.now(UTC)),
        )
        conn.commit()


def save_tls_change(host: str, port: int, fingerprint: str) -> None:
    """Record that the host presented another certificate than the pinned one."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE tls_pins SET seen_fingerprint = %s, seen_at = %s WHERE host = %s AND port = %s",
            (fingerprint, datetime.now(UTC), host, port),
        )
        conn.commit()


def accept_tls_change(host: str, port: int) -> str | None:
    """Make the certificate last seen the pinned one. Returns its fingerprint."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tls_pins
            SET fingerprint = seen_fingerprint, source = 'accepted', pinned_at = %s,
                seen_fingerprint = NULL, seen_at = NULL
            WHERE host = %s AND port = %s AND seen_fingerprint IS NOT NULL
            RETURNING fingerprint
            """,
            (datetime.now(UTC), host, port),
        )
        row = cur.fetchone()
        conn.commit()
        return row["fingerprint"] if row else None


def dismiss_tls_change(host: str, port: int) -> bool:
    """Drop the record of a changed certificate; the pin stays as it is."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE tls_pins SET seen_fingerprint = NULL, seen_at = NULL"
            " WHERE host = %s AND port = %s AND seen_fingerprint IS NOT NULL",
            (host, port),
        )
        changed = cur.rowcount > 0
        conn.commit()
        return changed


def list_tls_pins() -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM tls_pins ORDER BY host, port")
        return list(cur.fetchall())


def bootstrap_fingerprints() -> set[str]:
    """Certificate fingerprints the bootstrap scripts read on the hosts themselves."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT fingerprint FROM clusters WHERE fingerprint <> ''"
            " UNION SELECT fingerprint FROM pbs_connections WHERE fingerprint <> ''"
        )
        return {row["fingerprint"].strip().upper() for row in cur.fetchall() if row["fingerprint"]}
