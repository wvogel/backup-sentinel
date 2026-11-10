"""Overall restore progress across a multi-disk guest: cumulative bytes read across
all disks (pbs-restore resets "read" per disk) over the guest's total disk size, so
the bar/ETA reflect the whole VM instead of restarting 0–100% per volume."""

import unittest
from unittest import mock

from app.services import restore_worker
from app.services.restore_server import overall_restore_fields

G = 1024**3


class OverallRestoreFieldsTests(unittest.TestCase):
    def test_percent_from_cumulative_over_total(self) -> None:
        out = overall_restore_fields(80 * G, 200 * G)
        self.assertEqual(out["percent"], 40)
        self.assertEqual(out["bytes_done"], 80 * G)
        self.assertEqual(out["bytes_total"], 200 * G)
        self.assertEqual(out["phase"], "restoring")

    def test_caps_at_100_and_finalizing(self) -> None:
        out = overall_restore_fields(205 * G, 200 * G)  # rounding/zeroes can overshoot
        self.assertEqual(out["percent"], 100)
        self.assertEqual(out["bytes_done"], 200 * G)  # clamped to the total
        self.assertEqual(out["phase"], "finalizing")

    def test_none_when_total_unknown(self) -> None:
        self.assertIsNone(overall_restore_fields(10 * G, None))
        self.assertIsNone(overall_restore_fields(10 * G, 0))


class ProgressUpdaterTests(unittest.TestCase):
    def test_cumulative_across_disks_is_monotonic(self) -> None:
        # Two 100G disks → 200G total. Feed disk0 0→100G, then disk1 0→100G, exactly
        # as pbs-restore logs it (read resets at the second disk).
        class Server:
            def restore_total_bytes(self, vmid, guest_type="qemu"):
                return 200 * G

        writes: list[dict] = []
        with mock.patch.object(restore_worker.db_restore, "update_run", lambda run_id, **kw: writes.append(kw)):
            on_progress = restore_worker._progress_updater(Server(), run_id=1, vmid=9000)
            for r in (0, 50 * G, 100 * G):
                on_progress({"drive": "drive-scsi0", "percent": 0, "bytes_done": r})
            for r in (0, 50 * G, 100 * G):  # second disk: "read" starts over at 0
                on_progress({"drive": "drive-scsi1", "percent": 0, "bytes_done": r})

        pcts = [w["percent"] for w in writes]
        self.assertEqual(pcts, [0, 25, 50, 50, 75, 100])  # climbs across BOTH disks
        self.assertEqual(pcts, sorted(pcts))  # never jumps back
        # The second disk entering at 0 must not drop below the first disk's finish.
        self.assertGreaterEqual(min(pcts[3:]), 50)

    def test_falls_back_to_per_disk_when_total_unknown(self) -> None:
        class Server:
            def restore_total_bytes(self, vmid, guest_type="qemu"):
                return 0  # can't determine the total yet

        writes: list[dict] = []
        with mock.patch.object(restore_worker.db_restore, "update_run", lambda run_id, **kw: writes.append(kw)):
            on_progress = restore_worker._progress_updater(Server(), run_id=1, vmid=9000)
            on_progress({"drive": "drive-scsi0", "percent": 7, "bytes_done": 7 * G})
        # No total → the raw per-disk percent is kept (no wrong overall number).
        self.assertEqual(writes[-1]["percent"], 7)


if __name__ == "__main__":
    unittest.main()
