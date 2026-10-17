"""Database tests against a real PostgreSQL.

They TRUNCATE all tables, so they only run when BSENTINEL_DB_TESTS=1 is set
(CI does this with a throwaway Postgres service). Locally:

    docker run -d --rm --name bs-test-db -p 55432:5432 \\
      -e POSTGRES_DB=t -e POSTGRES_USER=t -e POSTGRES_PASSWORD=t postgres:18
    BSENTINEL_DB_TESTS=1 BSENTINEL_DATABASE_URL=postgresql://t:t@localhost:55432/t pytest tests/test_db.py
"""

import os
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

if os.environ.get("BSENTINEL_DB_TESTS") != "1":
    raise unittest.SkipTest("set BSENTINEL_DB_TESTS=1 to run database tests (they truncate all tables)")

from app import db, db_governance
from app.services import sync_progress, topology_store
from app.services.vzdump_log import VmBackupRun

NOW = datetime.now(UTC).replace(second=0, microsecond=0)


def setUpModule() -> None:
    db.init_db()


def _vm(vmid: int, name: str) -> dict:
    return {"node_name": "pve1", "vmid": vmid, "name": name, "backup_kind": "daily", "last_backup_at": None}


def _event(vmid: int, started_at: datetime, size: int = 10_000) -> dict:
    return {"vmid": vmid, "started_at": started_at, "size_bytes": size, "status": "ok"}


class DbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE clusters RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE app_settings")
            conn.commit()
        db.create_cluster("Test", "test", "https://pve.example:8006", "secret")
        self.cid = db.get_cluster_by_slug("test")["id"]
        db.sync_cluster_inventory(self.cid, [{"name": "pve1", "status": "online"}], [_vm(100, "a"), _vm(101, "b")])

    def vm_row(self, vmid: int) -> dict | None:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM vms WHERE cluster_id = %s AND vmid = %s", (self.cid, vmid))
            return cur.fetchone()


class GoneVmTests(DbTestCase):
    def test_missing_vm_is_flagged_and_cleared_when_it_returns(self) -> None:
        db.sync_cluster_inventory(self.cid, [], [_vm(100, "a")])
        self.assertIsNotNone(self.vm_row(101)["missing_since"])
        db.sync_cluster_inventory(self.cid, [], [_vm(100, "a"), _vm(101, "b")])
        self.assertIsNone(self.vm_row(101)["missing_since"])

    def test_prune_deletes_only_gone_vms_without_backups(self) -> None:
        db.sync_cluster_inventory(self.cid, [], [_vm(100, "a"), _vm(101, "b"), _vm(102, "c")])
        db.sync_backup_events_for_source(self.cid, "pbs", [_event(101, NOW)])
        db.sync_cluster_inventory(self.cid, [], [_vm(100, "a")])  # 101 + 102 gone, 101 has a backup
        self.assertEqual(db.prune_gone_vms_without_backups(self.cid), 1)
        self.assertIsNone(self.vm_row(102))
        self.assertIsNotNone(self.vm_row(101))
        self.assertIsNotNone(self.vm_row(100))

    def test_governance_flags_gone_vms_and_hides_those_without_backups(self) -> None:
        db.sync_cluster_inventory(self.cid, [], [_vm(100, "a"), _vm(101, "b"), _vm(102, "c")])
        db.sync_backup_events_for_source(self.cid, "pbs", [_event(101, NOW)])
        db.sync_cluster_inventory(self.cid, [], [_vm(100, "a")])
        rows = {row["vmid"]: row for row in db.vm_governance_rows(self.cid)}
        self.assertEqual(set(rows), {100, 101})
        self.assertTrue(rows[101]["vm_gone"])
        self.assertEqual(rows[101]["backup_severity"], "ok")
        self.assertFalse(rows[100]["vm_gone"])


class StorageAndDetailTests(DbTestCase):
    def test_storages_are_replaced_on_every_sync(self) -> None:
        db.sync_backup_events_for_source(self.cid, "pbs", [_event(100, NOW)])
        db.update_backup_event_storages(
            self.cid, [{**_event(100, NOW), "storage": "pbs-a"}, {**_event(100, NOW), "storage": "pbs-b"}]
        )
        db.update_backup_event_storages(self.cid, [{**_event(100, NOW), "storage": "pbs-b"}])
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT storages FROM backup_events")
            self.assertEqual(cur.fetchone()["storages"], ["pbs-b"])

    def test_task_log_details_match_the_nearest_event(self) -> None:
        db.sync_backup_events_for_source(self.cid, "pbs", [_event(100, NOW), _event(100, NOW - timedelta(days=1))])
        runs = [
            VmBackupRun(vmid=100, started_at=NOW + timedelta(minutes=3), duration_seconds=305, reused_pct=90),
            VmBackupRun(vmid=100, started_at=NOW - timedelta(hours=5), duration_seconds=1),  # no event nearby
            VmBackupRun(vmid=100, started_at=NOW, failed=True),  # failed runs are ignored
        ]
        self.assertEqual(db.update_backup_event_details(self.cid, runs), 1)
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT started_at, duration_seconds, reused_pct FROM backup_events ORDER BY started_at")
            older, latest = cur.fetchall()
        self.assertEqual((latest["duration_seconds"], latest["reused_pct"]), (305, 90))
        self.assertIsNone(older["duration_seconds"])

    def test_failed_run_ends_its_in_progress_placeholder(self) -> None:
        # The sync saw the backup running (placeholder); the task log says it failed.
        started = NOW - timedelta(hours=1)
        db.sync_backup_events_for_source(self.cid, "pbs", [_event(100, started, size=1)])
        rows = {r["vmid"]: r for r in db.vm_governance_rows(self.cid)}
        self.assertIsNotNone(rows[100]["in_progress_since"])
        db.update_backup_event_details(self.cid, [VmBackupRun(vmid=100, started_at=started, failed=True)])
        rows = {r["vmid"]: r for r in db.vm_governance_rows(self.cid)}
        self.assertIsNone(rows[100]["in_progress_since"])
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT status, finished_at FROM backup_events")
            row = cur.fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIsNotNone(row["finished_at"])

    def test_processed_tasks_are_remembered(self) -> None:
        db.mark_backup_tasks_processed(self.cid, ["UPID:a", "UPID:b"])
        db.mark_backup_tasks_processed(self.cid, ["UPID:b"])
        self.assertEqual(db.processed_backup_task_upids(self.cid), {"UPID:a", "UPID:b"})

    def test_sparkline_carries_log_details(self) -> None:
        db.sync_backup_events_for_source(self.cid, "pbs", [_event(100, NOW)])
        db.update_backup_event_details(
            self.cid, [VmBackupRun(vmid=100, started_at=NOW, duration_seconds=305, full_backup=True)]
        )
        vm_id = self.vm_row(100)["id"]
        days = [d for d in db.vm_backup_sparkline_data(self.cid)[vm_id] if d["status"] == "ok"]
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0]["duration_seconds"], 305)
        self.assertIs(days[0]["full_backup"], True)


class TypicalDurationTests(DbTestCase):
    def test_median_of_recent_runs_needs_three_samples(self) -> None:
        days = [NOW - timedelta(days=i) for i in range(4)]
        db.sync_backup_events_for_source(self.cid, "pbs", [_event(100, d) for d in days] + [_event(101, NOW)])
        runs = [VmBackupRun(vmid=100, started_at=d, duration_seconds=s) for d, s in zip(days, (300, 310, 900, 290))]
        runs.append(VmBackupRun(vmid=101, started_at=NOW, duration_seconds=60))
        db.update_backup_event_details(self.cid, runs)
        self.assertEqual(db.typical_backup_durations(self.cid), {100: 305})


class RestoreClassTests(DbTestCase):
    def setUp(self) -> None:
        super().setUp()
        db.sync_backup_events_for_source(self.cid, "pbs", [_event(100, NOW), _event(101, NOW)])
        self.vm100 = self.vm_row(100)["id"]

    def rows(self) -> dict:
        return {row["vmid"]: row for row in db.vm_governance_rows(self.cid)}

    def test_never_tested_is_open_with_default_180_days(self) -> None:
        row = self.rows()[100]
        self.assertEqual(row["restore_due_status"], "open")
        self.assertEqual(row["restore_interval_days"], 180)
        self.assertEqual(row["restore_class"], "medium")

    def test_class_sets_interval_and_expiry(self) -> None:
        db.update_vm_restore_class(self.vm100, "high")
        db.create_restore_test(self.vm100, (NOW - timedelta(days=100)).date(), "passed", "full", 5)
        row = self.rows()[100]
        self.assertEqual(row["restore_interval_days"], 90)
        self.assertEqual(row["restore_due_status"], "critical")
        db.update_vm_restore_class(self.vm100, "low")
        self.assertEqual(self.rows()[100]["restore_due_status"], "ok")

    def test_none_class_needs_no_test_and_survives_backup_policy_changes(self) -> None:
        db.update_vm_restore_class(self.vm100, "none")
        db.update_vm_backup_policy(self.vm100, "weekly")
        row = self.rows()[100]
        self.assertEqual(row["restore_class"], "none")
        self.assertEqual(row["restore_due_status"], "ok")


class ThemeSettingsTests(DbTestCase):
    def test_no_settings_means_no_inline_css(self) -> None:
        from app.web.common import theme_css

        self.assertEqual(theme_css(), "")

    def test_overrides_render_css(self) -> None:
        from app.web.common import theme_css

        db.save_settings({"theme_preset": "smile_eyes", "theme_primary": "#000000"})
        self.assertIn("--header-bg:#000000", theme_css())

    def test_default_preset_without_overrides_needs_no_inline_css(self) -> None:
        from app.web.common import theme_css

        db.save_settings({"theme_preset": "smile_eyes", "theme_primary": ""})
        self.assertEqual(theme_css(), "")

    def test_garbage_preset_falls_back(self) -> None:
        from app.services.theme_store import active_theme

        db.save_settings({"theme_preset": "x"})
        self.assertEqual(active_theme()[1].accent, "#ffff00")


APPEARANCE_FORM = {"preset": "smile_eyes", "primary": "", "accent": "", "background": "", "running": ""}


class AppearanceSettingsTests(DbTestCase):
    def save(self, **fields):
        from app.web.routes_settings import save_appearance

        return save_appearance({**APPEARANCE_FORM, **fields}, actor="tester")

    def test_valid_colours_are_normalised_and_audited(self) -> None:
        errors, warnings = self.save(primary=" #000 ")
        self.assertEqual((errors, warnings), ([], []))
        self.assertEqual(db.get_setting("theme_primary"), "#000000")
        keys = {entry["key"] for entry in db.list_settings_audit_entries(10)}
        self.assertIn("theme_primary", keys)

    def test_invalid_hex_saves_nothing(self) -> None:
        errors, _ = self.save(primary="#000000", accent="red")
        self.assertEqual(errors, ["accent"])
        self.assertEqual(db.get_setting("theme_primary"), "")

    def test_preset_values_are_not_stored_as_overrides(self) -> None:
        self.save(primary="#141450", accent="#FFFF00")
        self.assertEqual(db.get_setting("theme_primary"), "")
        self.assertEqual(db.get_setting("theme_accent"), "")

    def test_reset_clears_overrides(self) -> None:
        self.save(primary="#000000")
        self.save(reset="1", primary="#000000")
        self.assertEqual(db.get_setting("theme_primary"), "")

    def test_poor_contrast_warns_but_saves(self) -> None:
        errors, warnings = self.save(background="#ffff00", running="#ffff66")
        self.assertEqual(errors, [])
        self.assertIn("running", {w.key for w in warnings})
        self.assertEqual(db.get_setting("theme_background"), "#ffff00")

    def test_unknown_preset_is_an_error(self) -> None:
        errors, _ = self.save(preset="hacker")
        self.assertEqual(errors, ["preset"])


class AppearanceHttpTests(DbTestCase):
    def setUp(self) -> None:
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.web import routes_settings

        app = FastAPI()
        app.include_router(routes_settings.router)
        self.client = TestClient(app, follow_redirects=False)

    def post(self, **fields):
        return self.client.post("/settings/appearance", data={**APPEARANCE_FORM, **fields})

    def test_invalid_colour_redirects_with_field_error(self) -> None:
        response = self.post(primary="red")
        self.assertEqual(response.status_code, 303)
        self.assertIn("appearance_error=primary", response.headers["location"])
        page = self.client.get(response.headers["location"]).text
        self.assertIn("kein gültiger Farbwert", page)

    def test_contrast_warning_round_trip(self) -> None:
        response = self.post(background="#ffff00", running="#ffff66")
        self.assertIn("warn=running:", response.headers["location"])
        page = self.client.get(response.headers["location"]).text
        self.assertIn('class="warn-text"', page)

    def test_forged_warning_values_are_ignored(self) -> None:
        page = self.client.get("/settings?appearance=saved&warn=running:pwned:3.0,background:1.2:hello").text
        self.assertNotIn('class="warn-text"', page)

    def test_hex_field_accepts_pasted_values_with_spaces(self) -> None:
        page = self.client.get("/settings").text
        self.assertNotIn('maxlength="7"', page)


class DatastoreUsageDbTests(DbTestCase):
    def test_usage_is_replaced_per_pbs_and_listed_per_cluster(self) -> None:
        db.upsert_pbs_connection(
            self.cid, {"name": "pbs1", "api_url": "https://pbs1:8007", "api_token_id": "t", "api_token_secret": "s"}
        )
        pbs_id = db.list_pbs_connections(self.cid)[0]["id"]
        row = {"store": "nas-b1", "total_bytes": 100, "used_bytes": 62, "avail_bytes": 38, "estimated_full_at": None}
        db.replace_datastore_usage(pbs_id, [row, {**row, "store": "old"}])
        db.replace_datastore_usage(pbs_id, [{**row, "used_bytes": 70}])
        usage = db.list_datastore_usage(self.cid)
        self.assertEqual([(u["pbs_name"], u["store"], u["used_bytes"]) for u in usage], [("pbs1", "nas-b1", 70)])

    def test_past_full_estimate_is_not_listed(self) -> None:
        db.upsert_pbs_connection(
            self.cid, {"name": "pbs1", "api_url": "https://pbs1:8007", "api_token_id": "t", "api_token_secret": "s"}
        )
        pbs_id = db.list_pbs_connections(self.cid)[0]["id"]
        now = datetime.now(UTC)
        row = {"store": "past", "total_bytes": 100, "used_bytes": 75, "avail_bytes": 25, "estimated_full_at": None}
        db.replace_datastore_usage(
            pbs_id,
            [
                {**row, "estimated_full_at": now - timedelta(days=180)},
                {**row, "store": "soon", "estimated_full_at": now + timedelta(days=30)},
            ],
        )
        full = {u["store"]: u["estimated_full_at"] for u in db.list_datastore_usage(self.cid)}
        self.assertIsNone(full["past"])
        self.assertIsNotNone(full["soon"])


PBS_FH_MUC = {"name": "pbs-fh-muc", "api_url": "https://pbs:8007", "api_token_id": "t", "api_token_secret": "s"}


class PbsNamespaceTests(DbTestCase):
    PBS = PBS_FH_MUC

    def namespace(self) -> str:
        return db.list_pbs_connections(self.cid)[0]["namespace"]

    def test_bootstrap_rerun_without_namespace_keeps_it(self) -> None:
        db.upsert_pbs_connection(self.cid, self.PBS)
        self.assertEqual(self.namespace(), "")
        pbs_id = db.list_pbs_connections(self.cid)[0]["id"]
        self.assertTrue(db.update_pbs_settings(self.cid, pbs_id, " /pano//muc/ ", ""))
        self.assertEqual(self.namespace(), "pano/muc")
        db.upsert_pbs_connection(self.cid, self.PBS)
        self.assertEqual(self.namespace(), "pano/muc")
        db.upsert_pbs_connection(self.cid, {**self.PBS, "namespace": ""})
        self.assertEqual(self.namespace(), "")

    def test_aliases_are_normalized(self) -> None:
        db.upsert_pbs_connection(self.cid, self.PBS)
        pbs_id = db.list_pbs_connections(self.cid)[0]["id"]
        self.assertTrue(db.update_pbs_settings(self.cid, pbs_id, "pano", "PBS-mar-lej, 10.20.0.2\npbs-mar-lej"))
        row = db.list_pbs_connections(self.cid)[0]
        self.assertEqual((row["namespace"], row["aliases"]), ("pano", ["pbs-mar-lej", "10.20.0.2"]))

    def test_namespace_of_other_cluster_is_not_changed(self) -> None:
        db.upsert_pbs_connection(self.cid, {**self.PBS, "namespace": "pano"})
        pbs_id = db.list_pbs_connections(self.cid)[0]["id"]
        self.assertFalse(db.update_pbs_settings(self.cid + 1, pbs_id, "x", ""))
        self.assertEqual(self.namespace(), "pano")


class TopologyDbTests(DbTestCase):
    def test_sync_jobs_and_targets_are_replaced(self) -> None:
        db.upsert_pbs_connection(self.cid, PBS_FH_MUC)
        pbs_id = db.list_pbs_connections(self.cid)[0]["id"]
        job = {
            "job_id": "s-pano",
            "direction": "pull",
            "store": "nas",
            "ns": "pano",
            "remote_name": "pano",
            "remote_host": "pbs-pano",
            "remote_fingerprint": "AA",
            "remote_store": "nas-pano",
            "remote_ns": "",
            "schedule": "daily",
            "last_run_state": "OK",
            "last_run_at": datetime.now(UTC),
            "next_run_at": None,
        }
        db.replace_pbs_sync_jobs(pbs_id, [job, {**job, "job_id": "old"}])
        db.replace_pbs_sync_jobs(pbs_id, [job])
        self.assertEqual([j["job_id"] for j in db.list_pbs_sync_jobs()], ["s-pano"])

        target = {"storage_id": "pbs", "server": "10.0.0.5", "fingerprint": "AA", "datastore": "nas", "namespace": ""}
        db.replace_pve_pbs_targets(self.cid, [target, {**target, "storage_id": "old"}])
        db.replace_pve_pbs_targets(self.cid, [target])
        self.assertEqual([t["storage_id"] for t in db.list_pve_pbs_targets(self.cid)], ["pbs"])

    def test_direct_backup_storages_per_guest(self) -> None:
        vms = [
            {**_vm(100, "a"), "backup_storages": ["pbs-main", "pbs-side"]},
            {**_vm(101, "b"), "backup_storages": ["pbs-main"]},
        ]
        db.sync_cluster_inventory(self.cid, [{"name": "pve1", "status": "online"}], vms)
        storages = db.job_storages_by_vmid(self.cid)
        self.assertEqual(storages, {100: ["pbs-main", "pbs-side"], 101: ["pbs-main"]})
        self.assertEqual(topology_store.guests_by_storage(storages), {"pbs-main": {100, 101}, "pbs-side": {100}})

    def test_present_guests_by_store(self) -> None:
        started = (datetime.now(UTC) - timedelta(hours=2)).replace(second=0, microsecond=0)
        events = [
            {**_event(100, started), "storage": "pbs-fh-muc/nas", "location": "pbs:pbs-fh-muc/nas"},
            {**_event(100, started), "storage": "pbs-aboe-muc/nas-off", "location": "pbs:pbs-aboe-muc/nas-off/fh"},
            {**_event(101, started), "storage": "pbs-fh-muc/nas", "location": "pbs:pbs-fh-muc/nas"},
        ]
        db.sync_backup_events_for_source(self.cid, "pbs", events)
        db.update_backup_event_storages(self.cid, events)
        self.assertEqual(
            db.present_guests_by_store(self.cid),
            {("pbs-fh-muc", "nas"): {100: started, 101: started}, ("pbs-aboe-muc", "nas-off"): {100: started}},
        )

    def test_guest_type_is_stored(self) -> None:
        vm = {
            "node_name": "n1",
            "vmid": 300,
            "name": "ct",
            "guest_type": "lxc",
            "backup_kind": "daily",
            "last_backup_at": None,
        }
        db.sync_cluster_inventory(self.cid, [{"name": "n1", "status": "online"}], [vm])
        rows = {r["vmid"]: r for r in db.vm_governance_rows(self.cid)}
        self.assertEqual(rows[300]["guest_type"], "lxc")


class CopiesAndVerifyTests(DbTestCase):
    def sync(self, events):
        pbs = [e for e in events if e.get("source", "pbs") == "pbs"]
        db.sync_backup_events_for_source(self.cid, "pbs", pbs)
        db.update_backup_event_storages(self.cid, events)

    def row(self, vmid=100):
        return {r["vmid"]: r for r in db.vm_governance_rows(self.cid)}[vmid]

    def test_copies_count_distinct_servers_of_the_last_week(self) -> None:
        main = {"storage": "pbs-main", "location": "pbs:aa/nas-b1"}
        alias = {"storage": "pbs-main/nas-b1", "location": "pbs:aa/nas-b1/ns"}
        offsite = {"storage": "pbs-offsite", "location": "pbs:bb/backup"}
        self.sync(
            [
                {**_event(100, NOW), **main},
                {**_event(100, NOW), **alias},
                {**_event(100, NOW - timedelta(days=1)), **offsite},
                {**_event(101, NOW), **main},
                {**_event(101, NOW - timedelta(days=20)), **offsite},  # too old
            ]
        )
        self.assertEqual(self.row(100)["copy_count"], 2)
        self.assertEqual(self.row(101)["copy_count"], 1)
        self.assertEqual(self.row(101)["copy_status"], "single")
        single_copy_verified = {**self.row(101), "verify_status": "ok"}
        self.assertFalse(db_governance.is_problem(single_copy_verified))

    def test_verify_status(self) -> None:
        self.sync(
            [
                {**_event(100, NOW), "verify_state": "failed", "location": "pbs:aa/x"},
                {**_event(101, NOW - timedelta(days=40)), "verify_state": "ok", "location": "pbs:aa/x"},
            ]
        )
        self.assertEqual(self.row(100)["verify_status"], "critical")
        self.assertEqual(self.row(101)["verify_status"], "warning")

    def test_recently_verified_is_ok(self) -> None:
        self.sync([{**_event(100, NOW - timedelta(days=2)), "verify_state": "ok", "location": "pbs:aa/x"}])
        row = self.row(100)
        self.assertEqual(row["verify_status"], "ok")
        self.assertIsNotNone(row["last_verified_at"])


class ProblemFlagTests(DbTestCase):
    def test_problem_flag(self) -> None:
        healthy = {"location": "pbs:aa/x", "storage": "a", "verify_state": "ok"}
        db.sync_backup_events_for_source(
            self.cid,
            "pbs",
            [
                {**_event(100, NOW), **healthy},
                {**_event(100, NOW), "location": "pbs:bb/y", "storage": "b"},
                {**_event(101, NOW), **healthy, "verify_state": "failed"},
            ],
        )
        db.update_backup_event_storages(
            self.cid,
            [
                {**_event(100, NOW), **healthy},
                {**_event(100, NOW), "location": "pbs:bb/y", "storage": "b"},
                {**_event(101, NOW), **healthy},
                {**_event(101, NOW), "location": "pbs:bb/y", "storage": "b"},
            ],
        )
        rows = {r["vmid"]: r for r in db.vm_governance_rows(self.cid)}
        self.assertFalse(rows[100]["is_problem"])  # restore "open" alone is not a problem
        self.assertTrue(rows[101]["is_problem"])  # failed verify


class VmHistoryTests(DbTestCase):
    def setUp(self) -> None:
        super().setUp()
        db.sync_backup_events_for_source(
            self.cid,
            "pbs",
            [_event(100, NOW - timedelta(days=d), size=1000 + d) for d in range(3)]
            + [_event(100, NOW - timedelta(days=40))],
        )
        db.update_backup_event_details(
            self.cid, [VmBackupRun(vmid=100, started_at=NOW, duration_seconds=300, reused_pct=90, full_backup=False)]
        )
        self.vm_id = self.vm_row(100)["id"]

    def test_history_lists_the_last_30_days_oldest_first(self) -> None:
        history = db.vm_backup_history(self.vm_id, days=30)
        self.assertEqual([h["size_bytes"] for h in history], [1002, 1001, 1000])
        self.assertEqual((history[-1]["duration_seconds"], history[-1]["reused_pct"]), (300, 90))

    def test_history_has_one_entry_per_backup_day_for_hourly_backups(self) -> None:
        start = NOW - timedelta(days=10)
        db.sync_backup_events_for_source(
            self.cid, "pbs", [_event(101, start + timedelta(hours=h), size=2000 + h) for h in range(72)]
        )
        history = db.vm_backup_history(self.vm_row(101)["id"], days=30)
        self.assertLessEqual(len(history), 4)  # 72 hourly backups span 3–4 backup days
        self.assertEqual(sum(h["backups"] for h in history), 72)
        self.assertEqual(history[-1]["size_bytes"], 2071)  # latest backup of the last day

    def test_api_returns_history_only_for_vms_of_that_cluster(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.web import routes_clusters

        app = FastAPI()
        app.include_router(routes_clusters.router)
        client = TestClient(app)
        ok = client.get(f"/api/clusters/test/vms/{self.vm_id}/history")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(len(ok.json()["events"]), 3)
        self.assertEqual(client.get("/api/clusters/test/vms/999999/history").status_code, 404)


class WeeklyStatsTests(DbTestCase):
    def test_counts_failures_and_lists_full_runs_of_the_last_week(self) -> None:
        db.sync_backup_events_for_source(
            self.cid,
            "pbs",
            [
                _event(100, NOW),
                _event(101, NOW),
                {**_event(100, NOW - timedelta(days=1)), "status": "failed", "size_bytes": 0},
                {**_event(101, NOW - timedelta(days=10)), "status": "failed", "size_bytes": 0},
            ],
        )
        db.update_backup_event_details(self.cid, [VmBackupRun(vmid=101, started_at=NOW, full_backup=True)])
        stats = db.weekly_backup_stats(NOW - timedelta(days=7))
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["full_runs"], ["Test / b"])


if __name__ == "__main__":
    unittest.main()


class ClusterPageTests(DbTestCase):
    def setUp(self) -> None:
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.web import routes_clusters

        app = FastAPI()
        app.include_router(routes_clusters.router)
        self.client = TestClient(app, follow_redirects=False)

    def page(self) -> str:
        with mock.patch("app.web.routes_clusters.running_backup_progress_peek", return_value=(None, False)):
            response = self.client.get("/clusters/test")
        self.assertEqual(response.status_code, 200)
        return response.text

    def test_without_diagram_the_sync_dots_stay_in_the_header(self) -> None:
        db.upsert_pbs_connection(self.cid, PBS_FH_MUC)
        html = self.page()
        self.assertIn('class="sync-dots"', html)
        self.assertNotIn("data-topology", html)

    def test_diagram_carries_the_sync_dots(self) -> None:
        db.upsert_pbs_connection(self.cid, PBS_FH_MUC)
        pbs_id = db.list_pbs_connections(self.cid)[0]["id"]
        db.replace_datastore_usage(
            pbs_id,
            [{"store": "nas", "total_bytes": 100, "used_bytes": 40, "avail_bytes": 60, "estimated_full_at": None}],
        )
        target = {"storage_id": "pbs", "server": "pbs-fh-muc", "fingerprint": "", "datastore": "nas", "namespace": ""}
        db.replace_pve_pbs_targets(self.cid, [target])
        topology_store.refresh_all()
        html = self.page()
        self.assertIn("data-topology", html)
        self.assertNotIn('class="sync-dots"', html)
        self.assertEqual(html.count('class="sync-dot '), 2)  # PVE + pbs-fh-muc

    def running_sync(self) -> tuple[int, str]:
        """A sync job running right now."""
        sync_progress.clear()
        db.upsert_pbs_connection(self.cid, PBS_FH_MUC)
        pbs_id = db.list_pbs_connections(self.cid)[0]["id"]
        db.replace_datastore_usage(
            pbs_id,
            [{"store": "nas", "total_bytes": 100, "used_bytes": 40, "avail_bytes": 60, "estimated_full_at": None}],
        )
        target = {"storage_id": "pbs", "server": "pbs-fh-muc", "fingerprint": "", "datastore": "nas", "namespace": ""}
        db.replace_pve_pbs_targets(self.cid, [target])
        upid = "UPID:pbs-fh-muc:0000ABCD:0012F00D:00000002:68F15A40:syncjob:s:sync@pbs:"
        job = {
            "job_id": "s",
            "direction": "pull",
            "store": "copy",
            "ns": "",
            "remote_name": "",
            "remote_host": "",
            "remote_fingerprint": "",
            "remote_store": "nas",
            "remote_ns": "",
            "schedule": "daily",
            "last_run_state": "",
            "last_run_at": None,
            "next_run_at": None,
            "running_since": datetime.now(UTC),
            "running_upid": upid,
        }
        db.replace_pbs_sync_jobs(pbs_id, [job])
        topology_store.refresh_all()
        return pbs_id, upid

    @staticmethod
    def ring(html: str) -> str:
        start = html.rindex("<span", 0, html.index('data-sync-ring="'))
        return "".join(html[start : html.index("</span>", html.index("running-ring-label", start))].split())

    def test_live_sync_progress(self) -> None:
        pbs_id, upid = self.running_sync()
        with mock.patch("app.services.sync_progress.sync_task_state", return_value=(45.31, False)) as state:
            response = self.client.get("/api/clusters/test/sync-progress")
        self.assertEqual(response.json(), {f"{pbs_id}:s": {"percent": 45.31, "finished": False}})
        self.assertEqual(state.call_args.args[1], upid)
        # The page shows that live value.
        self.assertTrue(self.ring(self.page()).endswith(">45%"))

    def test_page_without_live_value_shows_no_stale_percent(self) -> None:
        self.running_sync()
        ring = self.ring(self.page())
        self.assertIn("is-indeterminate", ring)
        self.assertFalse(ring.endswith("%"))

    def test_placeholder_is_not_running_when_live_data_says_otherwise(self) -> None:
        db.sync_backup_events_for_source(self.cid, "pbs", [_event(100, NOW - timedelta(minutes=30), size=1)])
        with mock.patch("app.web.routes_clusters.running_backup_progress_peek", return_value=({}, True)):
            html = self.client.get("/clusters/test").text
        self.assertNotIn("is-running", html)
        # PVE unreachable: the placeholder is all there is to go on.
        with mock.patch("app.web.routes_clusters.running_backup_progress_peek", return_value=(None, False)):
            html = self.client.get("/clusters/test").text
        self.assertIn("is-running", html)

    def test_overview_shows_running_replication(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.web import routes_dashboard

        self.running_sync()
        app = FastAPI()
        app.include_router(routes_dashboard.router)
        with mock.patch("app.web.routes_dashboard.cached_running_backups", return_value={}):
            html = TestClient(app).get("/").text
        self.assertIn("Replikation läuft", html)
        self.assertIn("pbs-fh-muc/nas → copy", html)

    def test_qdevice_in_the_nodes_tile(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.web import routes_dashboard

        db.update_cluster_qdevice(self.cid, {"state": "Connected", "host": "10.0.0.9:5403"})
        html = self.page()
        self.assertIn('class="qdevice-tag"', html)
        self.assertIn("QNetd-Host: 10.0.0.9:5403", html)
        app = FastAPI()
        app.include_router(routes_dashboard.router)
        with mock.patch("app.web.routes_dashboard.cached_running_backups", return_value={}):
            self.assertIn('class="qdevice-tag"', TestClient(app).get("/").text)
        # Not connected: the tile turns yellow; none: no hint at all.
        db.update_cluster_qdevice(self.cid, {"state": "Disconnected", "host": "10.0.0.9:5403"})
        self.assertIn('class="cluster-stat is-warn"', self.page())
        db.update_cluster_qdevice(self.cid, {})
        self.assertNotIn('class="qdevice-tag"', self.page())

    def test_ha_tag_with_placement(self) -> None:
        db.update_vm_ha(self.cid, {100: {"state": "started", "placement": "only", "nodes": ["pve1 (2)", "pve2"]}})
        html = self.page()
        self.assertIn("ha-tag is-restricted", html)
        self.assertIn("Darf nur laufen auf: pve1 (2), pve2", html)
        self.assertIn("Zustand: gestartet", html)
        # No longer HA-managed: the tag goes.
        db.update_vm_ha(self.cid, {})
        self.assertNotIn("ha-tag", self.page())

    def test_template_tag(self) -> None:
        vms = [{**_vm(100, "tpl-debian"), "is_template": True}, _vm(101, "b")]
        db.sync_cluster_inventory(self.cid, [{"name": "pve1", "status": "online"}], vms)
        html = self.page()
        self.assertEqual(html.count('class="kind-tag tpl-tag"'), 1)

    def test_overview_sync_freshness_dot(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.web import routes_dashboard

        app = FastAPI()
        app.include_router(routes_dashboard.router)
        with mock.patch("app.web.routes_dashboard.cached_running_backups", return_value={}):
            html = TestClient(app).get("/").text
        self.assertIn("sync-fresh sync-fresh-critical", html)  # never synced
        db.update_cluster_sync_status(self.cid, True, "ok")
        with mock.patch("app.web.routes_dashboard.cached_running_backups", return_value={}):
            html = TestClient(app).get("/").text
        self.assertIn("sync-fresh sync-fresh-ok", html)
        self.assertNotIn('class="muted cluster-sync-meta"', html)

    def test_sync_target_namespaces_come_from_the_current_sync_jobs(self) -> None:
        db.upsert_pbs_connection(self.cid, PBS_FH_MUC)
        pbs_id = db.list_pbs_connections(self.cid)[0]["id"]
        target = {"storage_id": "pbs", "server": "pbs-fh-muc", "fingerprint": "", "datastore": "nas", "namespace": ""}
        db.replace_pve_pbs_targets(self.cid, [target])
        job = {
            "job_id": "s",
            "direction": "pull",
            "store": "nas-off",
            "ns": "mar-lej",
            "remote_name": "",
            "remote_host": "",
            "remote_fingerprint": "",
            "remote_store": "nas",
            "remote_ns": "",
            "schedule": "daily",
            "last_run_state": "OK",
            "last_run_at": datetime.now(UTC),
            "next_run_at": None,
        }
        db.replace_pbs_sync_jobs(pbs_id, [job])
        # A diagram stored by an older version lacks the target connection:
        # it must not hide the namespace from the next sync.
        db.save_cluster_topology(self.cid, '{"columns": [], "edges": [], "lines": []}')
        self.assertEqual(topology_store.sync_target_namespaces(self.cid), {pbs_id: {"nas-off": {"mar-lej"}}})

    def test_weekly_sync_keeps_its_copy_counted_longer(self) -> None:
        db.upsert_pbs_connection(self.cid, {**PBS_FH_MUC, "name": "pbs-aboe-muc", "api_url": "https://aboe:8007"})
        pbs_id = db.list_pbs_connections(self.cid)[0]["id"]
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        fresh, weekly_copy = now - timedelta(days=1), now - timedelta(days=8)
        events = [
            {**_event(100, fresh), "storage": "a", "location": "pbs:mgm-pbs-mar-lej/nas"},
            {**_event(100, weekly_copy), "storage": "b", "location": "pbs:pbs-aboe-muc/nas-off/mar-lej"},
        ]
        db.sync_backup_events_for_source(self.cid, "pbs", events)
        db.update_backup_event_storages(self.cid, events)
        row = {r["vmid"]: r for r in db.vm_governance_rows(self.cid)}[100]
        self.assertEqual(row["copy_count"], 1)  # 8 days: too old for the default 7
        job = {
            "job_id": "s",
            "direction": "pull",
            "store": "nas-off",
            "ns": "mar-lej",
            "remote_name": "lej",
            "remote_host": "",
            "remote_fingerprint": "",
            "remote_store": "nas",
            "remote_ns": "",
            "schedule": "sat 2:00",
            "last_run_state": "OK",
            "last_run_at": now,
            "next_run_at": None,
        }
        db.replace_pbs_sync_jobs(pbs_id, [job])
        row = {r["vmid"]: r for r in db.vm_governance_rows(self.cid)}[100]
        self.assertEqual(row["copy_count"], 2)  # fed weekly: 9 days count
        self.assertEqual([c["current"] for c in row["copy_history"]], [True, True])
