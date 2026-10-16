import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

from app.services import pbs

CONN = {"api_url": "https://pbs", "api_token_id": "id", "api_token_secret": "secret"}


class FetchAllPBSBackupsTests(unittest.TestCase):
    def test_failed_datastore_marks_result_incomplete(self) -> None:
        def fake_backups(conn, vmids, cutoff, datastore=""):
            if datastore == "broken":
                raise pbs.PBSSyncError("timeout")
            return [{"vmid": 100}]

        with (
            mock.patch.object(pbs, "fetch_pbs_datastores", return_value=(["ok", "broken"], "", True)),
            mock.patch.object(pbs, "fetch_pbs_backups", side_effect=fake_backups),
        ):
            events, _logs, complete = pbs.fetch_all_pbs_backups(CONN, {100})
        self.assertEqual(events, [{"vmid": 100}])
        self.assertFalse(complete)

    def test_datastore_listing_error_is_incomplete(self) -> None:
        with mock.patch.object(pbs, "_pbs_get_json", side_effect=pbs.PBSSyncError("down")):
            names, _debug, ok = pbs.fetch_pbs_datastores(CONN)
        self.assertEqual(names, [])
        self.assertFalse(ok)

    def test_empty_pbs_without_errors_is_complete(self) -> None:
        with mock.patch.object(pbs, "_pbs_get_json", return_value=[]):
            events, _logs, complete = pbs.fetch_all_pbs_backups(CONN, {100})
        self.assertEqual(events, [])
        self.assertTrue(complete)

    def test_events_carry_pbs_and_datastore_as_storage(self) -> None:
        snapshots = [
            {"backup-type": "vm", "backup-id": "121", "backup-time": 1_760_000_000, "size": 10, "comment": "mgmfw"}
        ]
        with mock.patch.object(pbs, "_pbs_get_json", return_value=snapshots):
            events = pbs.fetch_pbs_backups({**CONN, "name": "pbs-fh-muc"}, {121}, datastore="nas-b1")
        self.assertEqual([e["storage"] for e in events], ["pbs-fh-muc/nas-b1"])
        self.assertEqual([e["guest_name"] for e in events], ["mgmfw"])
        self.assertEqual([e["location"] for e in events], ["pbs:pbs-fh-muc/nas-b1"])

    def test_without_namespace_only_root_is_read(self) -> None:
        snap = {"backup-type": "vm", "backup-id": "121", "backup-time": 1_760_000_000, "size": 10}
        calls = []

        def fake_get(base_url, path, token_id, token_secret, **kwargs):
            calls.append((path, kwargs.get("query")))
            return [snap]

        with mock.patch.object(pbs, "_pbs_get_json", side_effect=fake_get):
            events = pbs.fetch_pbs_backups(CONN, {121}, datastore="nas-b1")
        self.assertEqual(len(events), 1)
        self.assertEqual(calls, [("/api2/json/admin/datastore/nas-b1/snapshots", None)])

    def test_namespace_subtree_is_read(self) -> None:
        snap = {"backup-type": "vm", "backup-id": "121", "backup-time": 1_760_000_000, "size": 10}
        calls = []

        def fake_get(base_url, path, token_id, token_secret, **kwargs):
            calls.append((path, kwargs.get("query")))
            if path.endswith("/namespace"):
                return [{"ns": ""}, {"ns": "pano"}, {"ns": "pano/muc"}, {"ns": "panorama"}, {"ns": "other"}]
            ns = (kwargs.get("query") or {}).get("ns", "")
            return [snap] if ns == "pano/muc" else []

        conn = {**CONN, "name": "pbs-fh-muc", "namespace": "pano"}
        with mock.patch.object(pbs, "_pbs_get_json", side_effect=fake_get):
            events = pbs.fetch_pbs_backups(conn, {121}, datastore="nas-b1")
        self.assertEqual([e["location"] for e in events], ["pbs:pbs-fh-muc/nas-b1/pano/muc"])
        self.assertEqual([e["storage"] for e in events], ["pbs-fh-muc/nas-b1/pano/muc"])
        snapshot_queries = [q for path, q in calls if path.endswith("/snapshots")]
        self.assertEqual(snapshot_queries, [{"ns": "pano"}, {"ns": "pano/muc"}])

    def test_namespace_missing_on_datastore_is_skipped(self) -> None:
        def fake_get(base_url, path, token_id, token_secret, **kwargs):
            if path.endswith("/namespace"):
                return [{"ns": ""}, {"ns": "other"}]
            raise AssertionError("snapshots must not be read")

        with mock.patch.object(pbs, "_pbs_get_json", side_effect=fake_get):
            events = pbs.fetch_pbs_backups({**CONN, "namespace": "pano"}, {121}, datastore="nas-b1")
        self.assertEqual(events, [])

    def test_namespace_listing_failure_raises(self) -> None:
        with mock.patch.object(pbs, "_pbs_get_json", side_effect=pbs.PBSSyncError("timeout")):
            with self.assertRaises(pbs.PBSSyncError):
                pbs.fetch_pbs_backups({**CONN, "namespace": "pano"}, {121}, datastore="nas-b1")


class DatastoreUsageTests(unittest.TestCase):
    def test_parses_usage_and_full_estimate(self) -> None:
        rows = [
            {"store": "nas-b1", "total": 1000, "used": 620, "avail": 380, "estimated-full-date": 1801000000},
            {"store": "nas-off", "total": 500, "used": 10, "avail": 490, "estimated-full-date": 0},
            {"store": "broken", "error": "not mounted"},
        ]
        with mock.patch.object(pbs, "_pbs_get_json", return_value=rows):
            usage = pbs.fetch_datastore_usage(CONN)
        self.assertEqual([u["store"] for u in usage], ["nas-b1", "nas-off"])
        self.assertEqual(usage[0]["used_bytes"], 620)
        self.assertEqual(usage[0]["estimated_full_at"].year, 2027)
        self.assertIsNone(usage[1]["estimated_full_at"])

    def test_past_full_estimate_is_dropped(self) -> None:
        rows = [{"store": "nas-pve-fh-muc", "total": 100, "used": 75, "avail": 25, "estimated-full-date": 1776297600}]
        with mock.patch.object(pbs, "_pbs_get_json", return_value=rows):
            usage = pbs.fetch_datastore_usage(CONN)
        self.assertIsNone(usage[0]["estimated_full_at"])


class DatastoreAssessmentTests(unittest.TestCase):
    NOW = datetime(2026, 10, 11, tzinfo=UTC)

    def assess(self, used, full_in_days=None):
        full = self.NOW + timedelta(days=full_in_days) if full_in_days is not None else None
        return pbs.assess_datastore_usage(used, 100, full, self.NOW)

    def test_levels(self) -> None:
        self.assertEqual(self.assess(50), "ok")
        self.assertEqual(self.assess(80), "warning")
        self.assertEqual(self.assess(90), "critical")
        self.assertEqual(self.assess(30, full_in_days=40), "warning")
        self.assertEqual(self.assess(30, full_in_days=10), "critical")
        self.assertEqual(self.assess(30, full_in_days=400), "ok")

    def test_past_full_estimate_is_ignored(self) -> None:
        # PBS extrapolates shrinking usage backwards: a past date means "not filling up".
        self.assertEqual(self.assess(75, full_in_days=-180), "ok")

    def test_unknown_total_is_ok(self) -> None:
        self.assertEqual(pbs.assess_datastore_usage(0, 0, None, self.NOW), "ok")


REMOTES = [{"name": "pano", "host": "pbs-pano.int.example.de", "fingerprint": "AA:BB", "auth-id": "sync@pbs"}]


class SyncJobTests(unittest.TestCase):
    def fetch(self, jobs, remotes=None, direction_error=False):
        calls = []

        def fake_get(base_url, path, token_id, token_secret, **kwargs):
            calls.append((path, kwargs.get("query")))
            if path.endswith("/config/remote"):
                return REMOTES if remotes is None else remotes
            if direction_error and kwargs.get("query"):
                raise pbs.PBSSyncError("PBS API returned HTTP 400 for /admin/sync: schema does not allow")
            return jobs

        with mock.patch.object(pbs, "_pbs_get_json", side_effect=fake_get):
            return pbs.fetch_pbs_sync_jobs(CONN), calls

    def test_pull_job_with_remote(self) -> None:
        jobs = [
            {
                "id": "s-pano",
                "store": "nas-pve-fh-muc",
                "ns": "pano",
                "remote": "pano",
                "remote-store": "nas-pbs-pano",
                "schedule": "daily",
                "last-run-state": "OK",
                "last-run-endtime": 1_760_000_000,
                "next-run": 1_760_086_400,
            }
        ]
        result, calls = self.fetch(jobs)
        self.assertEqual(calls[0], ("/api2/json/admin/sync", {"sync-direction": "all"}))
        job = result[0]
        self.assertEqual(job["direction"], "pull")
        self.assertEqual((job["store"], job["ns"]), ("nas-pve-fh-muc", "pano"))
        self.assertEqual(
            (job["remote_name"], job["remote_host"], job["remote_fingerprint"]),
            ("pano", "pbs-pano.int.example.de", "AA:BB"),
        )
        self.assertEqual((job["remote_store"], job["remote_ns"]), ("nas-pbs-pano", ""))
        self.assertEqual(job["last_run_state"], "OK")
        self.assertEqual(job["last_run_at"], datetime.fromtimestamp(1_760_000_000, UTC))
        self.assertEqual(job["next_run_at"], datetime.fromtimestamp(1_760_086_400, UTC))

    def test_push_and_local_jobs(self) -> None:
        jobs = [
            {"id": "p", "store": "a", "remote": "pano", "remote-store": "b", "sync-direction": "push"},
            {"id": "l", "store": "copy", "remote-store": "main"},
        ]
        result, _ = self.fetch(jobs)
        self.assertEqual([j["direction"] for j in result], ["push", "pull"])
        self.assertEqual((result[1]["remote_name"], result[1]["remote_host"]), ("", ""))
        self.assertIsNone(result[1]["last_run_at"])

    def test_old_pbs_without_direction_parameter(self) -> None:
        result, calls = self.fetch(
            [{"id": "x", "store": "a", "remote": "pano", "remote-store": "b"}], direction_error=True
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(calls[1], ("/api2/json/admin/sync", None))

    def test_running_job_has_no_end_but_a_start_from_its_upid(self) -> None:
        # PBS reports only the UPID while a job runs; start time is hex field 5.
        upid = "UPID:pbs-fh-muc:0000ABCD:0012F00D:00000002:68F15A40:syncjob:s-pano:sync@pbs:"
        result, _ = self.fetch(
            [{"id": "s", "store": "a", "remote": "pano", "remote-store": "b", "last-run-upid": upid}]
        )
        self.assertEqual(result[0]["running_since"], datetime.fromtimestamp(0x68F15A40, UTC))
        self.assertIsNone(result[0]["last_run_at"])

    def test_finished_job_is_not_running(self) -> None:
        upid = "UPID:pbs:0000ABCD:0012F00D:00000002:68F15A40:syncjob:s:sync@pbs:"
        job = {
            "id": "s",
            "store": "a",
            "remote-store": "b",
            "last-run-upid": upid,
            "last-run-endtime": 1_760_000_000,
            "last-run-state": "OK",
        }
        result, _ = self.fetch([job])
        self.assertIsNone(result[0]["running_since"])

    def test_unknown_remote_keeps_name(self) -> None:
        result, _ = self.fetch([{"id": "x", "store": "a", "remote": "gone", "remote-store": "b"}], remotes=[])
        self.assertEqual((result[0]["remote_name"], result[0]["remote_host"]), ("gone", ""))


class CertFingerprintTests(unittest.TestCase):
    def test_fingerprints_of_the_pbs_certificates(self) -> None:
        rows = [
            {"filename": "proxy.pem", "fingerprint": "ab:cd:ef"},
            {"filename": "proxy.pem", "fingerprint": ""},
        ]
        with mock.patch.object(pbs, "_pbs_get_json", return_value=rows) as get:
            self.assertEqual(pbs.fetch_pbs_cert_fingerprints(CONN), ["ab:cd:ef"])
        self.assertEqual(get.call_args.args[1], "/api2/json/nodes/localhost/certificates/info")


class SyncProgressTests(unittest.TestCase):
    UPID = "UPID:pbs-fh-muc:0000ABCD:0012F00D:00000002:68F15A40:syncjob:s-pano:sync@pbs:"

    def state(self, total, tail, active=None):
        calls = []

        def fake(conn, path, **kwargs):
            calls.append(kwargs.get("query"))
            if kwargs["query"]["limit"] == "1":
                head = {"data": [{"n": 1, "t": "Starting"}], "total": total}
                if active is not None:
                    head["active"] = active
                return head
            return {"data": tail}

        with mock.patch.object(pbs, "_pbs_get_payload", side_effect=fake):
            return pbs.sync_task_state(CONN, self.UPID), calls

    def test_reads_only_the_end_of_a_long_log(self) -> None:
        tail = [
            {"n": 4801, "t": "percentage done: 45.31% (9/20 groups, 3/10 snapshots in group #10)"},
            {"n": 4802, "t": "sync snapshot vm/121/2026-10-16T20:00:01Z"},
        ]
        (percent, finished), calls = self.state(5000, tail, active=True)
        self.assertEqual((percent, finished), (45.31, False))
        self.assertEqual(calls[0], {"start": "0", "limit": "1", "test-status": "1"})
        self.assertEqual(calls[1], {"start": "4800", "limit": "200"})

    def test_finished_task_from_test_status(self) -> None:
        (percent, finished), _ = self.state(
            10, [{"n": 9, "t": "percentage done: 100.00% (20/20 groups)"}], active=False
        )
        self.assertEqual((percent, finished), (100.0, True))

    def test_finished_task_from_end_marker_on_older_pbs(self) -> None:
        tail = [{"n": 9, "t": "percentage done: 99.00% (19/20 groups)"}, {"n": 10, "t": "TASK OK"}]
        (_, finished), _ = self.state(10, tail)
        self.assertTrue(finished)

    def test_no_percentage_yet(self) -> None:
        (percent, finished), _ = self.state(1, [{"n": 1, "t": "Starting"}], active=True)
        self.assertEqual((percent, finished), (None, False))

    def test_running_job_carries_its_upid(self) -> None:
        def fake_get(base_url, path, token_id, token_secret, **kwargs):
            if path.endswith("/config/remote"):
                return []
            return [{"id": "s", "store": "a", "remote-store": "b", "last-run-upid": self.UPID}]

        with mock.patch.object(pbs, "_pbs_get_json", side_effect=fake_get):
            job = pbs.fetch_pbs_sync_jobs(CONN)[0]
        self.assertEqual(job["running_upid"], self.UPID)
        self.assertNotIn("running_percent", job)
