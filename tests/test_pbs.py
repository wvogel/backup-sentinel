import unittest
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
        snapshots = [{"backup-type": "vm", "backup-id": "121", "backup-time": 1_760_000_000, "size": 10}]
        with mock.patch.object(pbs, "_pbs_get_json", return_value=snapshots):
            events = pbs.fetch_pbs_backups({**CONN, "name": "pbs-fh-muc"}, {121}, datastore="nas-b1")
        self.assertEqual([e["storage"] for e in events], ["pbs-fh-muc/nas-b1"])
