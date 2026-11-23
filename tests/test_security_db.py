"""Secrets at rest, expiring enrollment, the audit hash chain and report hashes.

Like test_db.py these TRUNCATE tables, so they only run with BSENTINEL_DB_TESTS=1.
"""

import os
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest import mock

import psycopg
from fastapi import FastAPI
from fastapi.testclient import TestClient

if os.environ.get("BSENTINEL_DB_TESTS") != "1":
    raise unittest.SkipTest("database tests need BSENTINEL_DB_TESTS=1")

from app import db
from app.services import report_archive
from app.services.crypto import is_encrypted
from app.web.routes_bootstrap import router

PBS = {
    "name": "pbs1",
    "api_url": "https://pbs.example:8007",
    "api_token_id": "sentinel@pbs!t",
    "api_token_secret": "pbs-token-secret",
    "fingerprint": "",
}


def setUpModule() -> None:
    db.init_db()


def _raw(query: str, params: tuple = ()) -> list[dict]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


class SecurityDbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE clusters, monthly_reports RESTART IDENTITY CASCADE")
            conn.commit()
        db.create_cluster("Test", "test", "https://pve.example:8006", "enroll-secret")
        self.cluster = db.get_cluster_by_slug("test")
        self.client = TestClient(_app())


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


class SecretsAtRestTests(SecurityDbTestCase):
    def test_tokens_are_stored_encrypted_and_read_back_plain(self) -> None:
        db.finalize_cluster_registration(
            {
                "cluster_slug": "test",
                "api_user": "u@pve",
                "token_id": "t",
                "token_secret": "pve-token",
                "fingerprint": "",
            }
        )
        db.upsert_pbs_connection(self.cluster["id"], PBS)

        stored = _raw("SELECT enrollment_secret, token_secret FROM clusters")[0]
        pbs = _raw("SELECT api_token_secret FROM pbs_connections")[0]
        for value in (stored["enrollment_secret"], stored["token_secret"], pbs["api_token_secret"]):
            self.assertTrue(is_encrypted(value))
        for plain in ("enroll-secret", "pve-token", "pbs-token-secret"):
            self.assertNotIn(plain, [*stored.values(), *pbs.values()])

        cluster = db.get_cluster_by_slug("test")
        self.assertEqual(cluster["token_secret"], "pve-token")
        self.assertEqual(cluster["enrollment_secret"], "enroll-secret")
        self.assertEqual(db.list_clusters()[0]["token_secret"], "pve-token")
        self.assertEqual(db.list_pbs_connections()[0]["api_token_secret"], "pbs-token-secret")

    def test_plaintext_from_older_versions_is_encrypted_once(self) -> None:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE clusters SET token_secret = 'old-plain', enrollment_secret = 'old-enroll'")
            conn.commit()
        self.assertEqual(db.encrypt_stored_secrets(), 2)
        self.assertEqual(db.encrypt_stored_secrets(), 0)  # idempotent
        stored = _raw("SELECT token_secret FROM clusters")[0]["token_secret"]
        self.assertTrue(is_encrypted(stored))
        self.assertEqual(db.get_cluster_by_slug("test")["token_secret"], "old-plain")


class EnrollmentTests(SecurityDbTestCase):
    def _post(self, secret: str = "enroll-secret", **extra) -> int:
        body = {"cluster_slug": "test", "enrollment_secret": secret, **PBS, **extra}
        with mock.patch("app.web.routes_bootstrap.sync_executor"):
            return self.client.post("/api/bootstrap/pbs-finalize", json=body).status_code

    def test_valid_secret_registers_the_pbs(self) -> None:
        self.assertEqual(self._post(), 200)
        self.assertEqual(len(db.list_pbs_connections()), 1)

    def test_get_with_secrets_in_the_url_is_gone(self) -> None:
        response = self.client.get(
            "/api/bootstrap/pbs-finalize", params={"cluster_slug": "test", "enrollment_secret": "enroll-secret"}
        )
        self.assertEqual(response.status_code, 405)

    def test_wrong_secret_is_refused(self) -> None:
        self.assertEqual(self._post("nope"), 403)
        self.assertEqual(db.list_pbs_connections(), [])

    def test_expired_secret_is_refused_until_renewed(self) -> None:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE clusters SET enrollment_expires_at = %s", (datetime.now(UTC) - timedelta(minutes=1),))
            conn.commit()
        self.assertEqual(self._post(), 403)
        db.renew_enrollment(self.cluster["id"], "fresh-secret")
        self.assertEqual(self._post(), 403)  # the old one is gone for good
        self.assertEqual(self._post("fresh-secret"), 200)

    def test_secret_from_before_expiry_existed_counts_as_expired(self) -> None:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE clusters SET enrollment_expires_at = NULL")
            conn.commit()
        self.assertEqual(self._post(), 403)

    def test_only_https_targets(self) -> None:
        self.assertEqual(self._post(api_url="http://pbs.example:8007"), 400)

    def test_pve_finalize_needs_a_valid_secret_too(self) -> None:
        body = {
            "cluster_slug": "test",
            "enrollment_secret": "nope",
            "proxmox_node": "pve1",
            "api_user": "u@pve",
            "token_id": "t",
            "token_secret": "s",
        }
        self.assertEqual(self.client.post("/api/bootstrap/finalize", json=body).status_code, 403)
        self.assertIsNone(db.get_cluster_by_slug("test")["registered_at"])


class AuditChainTests(SecurityDbTestCase):
    def test_chain_verifies_and_grows(self) -> None:
        before = db.verify_audit_log()
        self.assertTrue(before["ok"])
        db.append_audit_event("alice", "cluster.created", "test", "x")
        db.append_settings_audit_entries([{"actor": "alice", "key": "k1", "old_value": "", "new_value": "1"}] * 2)
        after = db.verify_audit_log()
        self.assertTrue(after["ok"])
        self.assertEqual(after["entries"], before["entries"] + 3)
        self.assertNotEqual(after["head"], before["head"])

    def test_database_refuses_changes_to_the_log(self) -> None:
        db.append_audit_event("alice", "cluster.created", "test", "x")
        for statement in (
            "UPDATE settings_audit_log SET actor = 'mallory'",
            "DELETE FROM settings_audit_log",
            "TRUNCATE settings_audit_log",
        ):
            with self.assertRaises(psycopg.Error), db.connect() as conn, conn.cursor() as cur:
                cur.execute(statement)
            with db.connect() as conn:
                conn.rollback()
        self.assertTrue(db.verify_audit_log()["ok"])

    def test_tampering_past_the_trigger_breaks_the_chain(self) -> None:
        db.append_audit_event("alice", "report.deleted", "2026-10/auto", "x")
        db.append_audit_event("alice", "cluster.created", "test", "y")
        with db.connect() as conn, conn.cursor() as cur:
            # What someone with database rights could do: switch the trigger off.
            cur.execute("ALTER TABLE settings_audit_log DISABLE TRIGGER settings_audit_log_no_change")
            cur.execute(
                "UPDATE settings_audit_log SET actor = 'mallory'"
                " WHERE id = (SELECT max(id) - 1 FROM settings_audit_log) RETURNING id"
            )
            changed = cur.fetchone()["id"]
            cur.execute("ALTER TABLE settings_audit_log ENABLE TRIGGER settings_audit_log_no_change")
            conn.commit()
        try:
            result = db.verify_audit_log()
            self.assertFalse(result["ok"])
            self.assertEqual(result["broken_at"], changed)
        finally:
            with db.connect() as conn, conn.cursor() as cur:  # leave an intact log for other tests
                cur.execute("ALTER TABLE settings_audit_log DISABLE TRIGGER settings_audit_log_no_change")
                cur.execute("UPDATE settings_audit_log SET actor = 'alice' WHERE id = %s", (changed,))
                cur.execute("ALTER TABLE settings_audit_log ENABLE TRIGGER settings_audit_log_no_change")
                conn.commit()
        self.assertTrue(db.verify_audit_log()["ok"])


class ReportIntegrityTests(SecurityDbTestCase):
    def _generate(self, tmp: str) -> dict:
        with mock.patch.object(report_archive, "REPORT_DIR", Path(tmp)):
            report_archive.generate_archived_report(
                date(2026, 10, 1), "manual", datetime(2026, 10, 20, tzinfo=UTC), actor="alice"
            )
        return db.get_monthly_report(db.list_monthly_reports()[0]["id"])

    def test_hashes_are_recorded_and_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            row = self._generate(tmp)
            self.assertEqual(row["sha256"]["all"], report_archive.file_sha256(row["pdf_path"]))
            self.assertEqual(row["sha256"]["json"], report_archive.file_sha256(row["json_path"]))
            self.assertTrue(report_archive.report_intact(row))
            newest = db.list_settings_audit_entries(1)[0]
            self.assertEqual((newest["actor"], newest["key"]), ("alice", "report.generated"))
            self.assertIn(row["sha256"]["all"], newest["new_value"])

    def test_a_changed_file_is_noticed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            row = self._generate(tmp)
            Path(row["pdf_path"]).write_bytes(b"%PDF-forged")
            self.assertIs(report_archive.file_intact(row, "all"), False)
            self.assertIs(report_archive.file_intact(row, "json"), True)
            self.assertFalse(report_archive.report_intact(row))

    def test_deleting_leaves_an_audit_entry_with_the_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            row = self._generate(tmp)
            self.assertTrue(report_archive.delete_archived_report(row["id"], "bob"))
            self.assertFalse(Path(row["pdf_path"]).exists())
            newest = db.list_settings_audit_entries(1)[0]
            self.assertEqual((newest["actor"], newest["key"]), ("bob", "report.deleted"))
            self.assertIn(row["sha256"]["all"], newest["new_value"])
            self.assertFalse(report_archive.delete_archived_report(row["id"], "bob"))

    def test_reports_from_before_hashes_are_sealed_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdf, js = Path(tmp) / "old.pdf", Path(tmp) / "old.json"
            pdf.write_bytes(b"%PDF-old")
            js.write_text("{}")
            db.archive_monthly_report(date(2026, 9, 1), "auto", pdf, js, {})
            self.assertEqual(report_archive.seal_archived_reports(), 1)
            self.assertEqual(report_archive.seal_archived_reports(), 0)
            row = db.list_monthly_reports()[0]
            self.assertEqual(row["sha256"]["all"], report_archive.file_sha256(pdf))
            self.assertTrue(report_archive.report_intact(row))
