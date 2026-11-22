"""Database tests for the NIS2 report queries, the archive and the report routes.

Like test_db.py, these TRUNCATE tables, so they only run with BSENTINEL_DB_TESTS=1.
"""

import os
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

if os.environ.get("BSENTINEL_DB_TESTS") != "1":
    raise unittest.SkipTest("set BSENTINEL_DB_TESTS=1 to run database tests (they truncate all tables)")

from app import db, db_restore
from app.services import nis2_report, report_archive


def setUpModule() -> None:
    db.init_db()


def _vm(vmid: int, name: str, kind: str = "daily") -> dict:
    return {"node_name": "pve1", "vmid": vmid, "name": name, "backup_kind": kind, "last_backup_at": None}


class Nis2DbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE clusters, monthly_reports RESTART IDENTITY CASCADE")
            conn.commit()
        db.create_cluster("Test", "test", "https://pve.example:8006", "secret")
        self.cid = db.get_cluster_by_slug("test")["id"]
        db.sync_cluster_inventory(
            self.cid, [{"name": "pve1", "status": "online"}], [_vm(100, "a"), _vm(101, "nojob", "none")]
        )
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, vmid FROM vms WHERE cluster_id = %s", (self.cid,))
            ids = {r["vmid"]: r["id"] for r in cur.fetchall()}
            self.a, self.nojob = ids[100], ids[101]
            cur.execute("UPDATE vms SET first_seen_at = %s", (datetime(2026, 9, 1, tzinfo=UTC),))
            t0 = datetime(2026, 9, 20, 21, tzinfo=UTC)
            for d in range(45):
                if d in (25, 26):  # 15.–16.10. missing: one gap
                    continue
                cur.execute(
                    "INSERT INTO backup_events (vm_id, started_at, finished_at, size_bytes, status, source)"
                    " VALUES (%s, %s, %s, %s, 'ok', 'pbs')",
                    (self.a, t0 + timedelta(days=d), t0 + timedelta(days=d, minutes=5), 10**9),
                )
            # a pruned backup still counts; a failed and a stub one do not
            cur.execute(
                "UPDATE backup_events SET removed_at = '2026-10-30' WHERE vm_id = %s AND started_at < '2026-10-01'",
                (self.a,),
            )
            cur.execute(
                "INSERT INTO backup_events (vm_id, started_at, size_bytes, status) VALUES (%s, %s, 0, 'failed')",
                (self.a, datetime(2026, 10, 15, 21, tzinfo=UTC)),
            )
            conn.commit()

    def test_first_seen_column_defaults_on_insert(self) -> None:
        db.sync_cluster_inventory(self.cid, [{"name": "pve1", "status": "online"}], [_vm(100, "a"), _vm(102, "new")])
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT first_seen_at FROM vms WHERE vmid = 102")
            self.assertIsNotNone(cur.fetchone()["first_seen_at"])

    def test_backup_times_include_latest_before_since(self) -> None:
        since = datetime(2026, 10, 1, tzinfo=UTC)
        times = db.nis2_backup_times(since)[self.a]
        self.assertEqual(times[0], datetime(2026, 9, 30, 21, tzinfo=UTC))
        self.assertTrue(all(t >= since for t in times[1:]))
        self.assertNotIn(datetime(2026, 10, 15, 21, tzinfo=UTC), times)

    def test_guest_rows_mark_unconfigured(self) -> None:
        kinds = {g["id"]: g["backup_kind"] for g in db.nis2_guest_rows()}
        self.assertEqual(kinds[self.nojob], "unconfigured")
        self.assertEqual(kinds[self.a], "daily")

    def test_protocol_has_failed_runs_and_documented_tests(self) -> None:
        run = db_restore.create_run(self.a)
        db_restore.update_run(run, status="running", started_at=datetime.now(UTC) - timedelta(minutes=5))
        db_restore.record_run_result(run, self.a, "failed", duration_seconds=300, checks={"error": "no ARP"})
        db.create_restore_test(self.a, date.today(), "passed", "file restore", 30, "Ticket 1")
        now = datetime.now(UTC)
        rows = db.nis2_restore_protocol(
            now - timedelta(days=1), now + timedelta(days=1), date.today(), date.today() + timedelta(days=1)
        )
        kinds = sorted((r["kind"], r["result"]) for r in rows)
        self.assertEqual(kinds, [("manual", "passed"), ("run", "failed")])
        # the documented test counts as evidence; the failed run does not
        self.assertEqual(db.nis2_restore_test_dates()[self.a], [date.today()])

    def test_report_on_real_queries(self) -> None:
        r = nis2_report.build_nis2_report(
            date(2026, 10, 1), trigger="auto", lang="de", now=datetime(2026, 11, 1, 6, tzinfo=UTC)
        )
        c = r["clusters"][0]
        self.assertEqual(c["vms"], 2)
        gaps = {g["vm_name"]: g for g in c["gaps"]}
        self.assertEqual(gaps["a"]["without_hours"], 72)
        self.assertEqual(gaps["a"]["over_hours"], 48)
        self.assertTrue(gaps["nojob"]["open"])
        self.assertEqual(c["backups_failed"], 1)

    def test_archive_writes_overall_and_cluster_pdfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(report_archive, "REPORT_DIR", Path(tmp)):
            pdf = report_archive.generate_archived_report(
                date(2026, 10, 1), "manual", datetime(2026, 10, 20, tzinfo=UTC)
            )
            self.assertTrue(pdf.exists())
            row = db.list_monthly_reports()[0]
            self.assertEqual(set(row["files"]), {"all", "test"})
            self.assertTrue(Path(row["files"]["test"]).exists())
            # a manual interim report does not count as the month's automatic one
            self.assertIsNone(db.get_monthly_report_for_month(date(2026, 10, 1)))
            report_archive.generate_archived_report(date(2026, 10, 1), "auto", datetime(2026, 11, 1, 6, tzinfo=UTC))
            self.assertIsNotNone(db.get_monthly_report_for_month(date(2026, 10, 1)))


class Nis2RoutesTestCase(Nis2DbTestCase):
    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient

        from app.main import app

        self.client = TestClient(app)

    def test_reports_page_and_preview(self) -> None:
        res = self.client.get("/reports")
        self.assertEqual(res.status_code, 200)
        self.assertIn("report-preview-tile", res.text)
        res = self.client.get("/reports/preview?cluster=test&month=2026-10")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Anhang", res.text)
        self.assertEqual(self.client.get("/reports/preview?cluster=nope").status_code, 404)
        self.assertEqual(self.client.get("/reports/preview?month=10-2026").status_code, 400)

    def test_download_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(report_archive, "REPORT_DIR", Path(tmp)):
            report_archive.generate_archived_report(date(2026, 10, 1), "manual", datetime(2026, 10, 20, tzinfo=UTC))
            rid = db.list_monthly_reports()[0]["id"]
            ok = self.client.get(f"/reports/archive/{rid}/pdf?scope=test")
            self.assertEqual(ok.status_code, 200)
            self.assertTrue(ok.content.startswith(b"%PDF"))
            self.assertIn("nis2-report-2026-10-test.pdf", ok.headers["content-disposition"])
            self.assertEqual(self.client.get(f"/reports/archive/{rid}/pdf?scope=other").status_code, 404)
            self.client.post(f"/reports/archive/{rid}/delete")
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
