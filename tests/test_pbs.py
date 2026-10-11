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

    def test_unknown_total_is_ok(self) -> None:
        self.assertEqual(pbs.assess_datastore_usage(0, 0, None, self.NOW), "ok")
