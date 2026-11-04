"""Overall restore progress across a multi-disk guest: weight each disk by its
virtual size (from the config) × its percent, over the fixed backup total — so the
bar/ETA reflect the whole VM instead of restarting 0–100% per volume."""

import unittest

from app.services.restore_server import overall_restore_percent, parse_config_disk_sizes

G = 1024**3


class ParseConfigDiskSizesTests(unittest.TestCase):
    def test_disks_only_with_drive_prefix(self) -> None:
        cfg = {
            "scsi0": "nas-restore:9000/vm-9000-disk-1.raw,size=120G",
            "scsi1": "nas-restore:9000/vm-9000-disk-2.raw,size=32G",
            "efidisk0": "nas-restore:9000/vm-9000-disk-0.raw,size=4M",
            "scsihw": "virtio-scsi-pci",  # controller, no size → dropped
            "net0": "virtio=AA:BB,bridge=vmbr9",  # not a disk → dropped
            "boot": "order=scsi0",
        }
        sizes = parse_config_disk_sizes(cfg)
        self.assertEqual(sizes["drive-scsi0"], 120 * G)
        self.assertEqual(sizes["drive-scsi1"], 32 * G)
        self.assertEqual(sizes["drive-efidisk0"], 4 * 1024**2)
        self.assertNotIn("drive-scsihw", sizes)
        self.assertNotIn("drive-net0", sizes)

    def test_junk_is_empty(self) -> None:
        self.assertEqual(parse_config_disk_sizes(None), {})
        self.assertEqual(parse_config_disk_sizes({}), {})


class OverallRestorePercentTests(unittest.TestCase):
    # The denominator is the SUM of all disk sizes (config holds every volume once
    # qmrestore allocated them), so no external backup total is needed — that total
    # is often absent, which is why depending on it fell back to per-disk 0–100%.
    def test_size_weighted_over_sum_of_all_disks(self) -> None:
        size = {"drive-scsi0": 120 * G, "drive-scsi1": 80 * G}  # sum 200G
        # disk0 done, disk1 half → (120 + 40) / 200 = 80%.
        out = overall_restore_percent({"drive-scsi0": 100, "drive-scsi1": 50}, size)
        self.assertEqual(out["percent"], 80)
        self.assertEqual(out["bytes_done"], 160 * G)
        self.assertEqual(out["bytes_total"], 200 * G)
        self.assertEqual(out["phase"], "restoring")

    def test_unstarted_disk_counts_as_zero_of_the_total(self) -> None:
        # 5 disks known; only disk0 (120G of 452G) done → ~26%, not 100%.
        size = {f"drive-scsi{i}": s * G for i, s in enumerate((120, 32, 150, 50, 100))}
        out = overall_restore_percent({"drive-scsi0": 100}, size)
        self.assertEqual(out["percent"], int(120 / 452 * 100))

    def test_monotonic_when_next_disk_enters_at_zero(self) -> None:
        size = {"drive-scsi0": 120 * G, "drive-scsi1": 80 * G}
        a = overall_restore_percent({"drive-scsi0": 100}, size)["percent"]  # 120/200 = 60%
        b = overall_restore_percent({"drive-scsi0": 100, "drive-scsi1": 0}, size)["percent"]
        self.assertEqual(a, 60)
        self.assertGreaterEqual(b, a)  # a new disk at 0% must not drop the bar

    def test_all_done_is_finalizing(self) -> None:
        size = {"drive-scsi0": 120 * G, "drive-scsi1": 80 * G}
        out = overall_restore_percent({"drive-scsi0": 100, "drive-scsi1": 100}, size)
        self.assertEqual(out["percent"], 100)
        self.assertEqual(out["phase"], "finalizing")

    def test_fallback_when_no_size_known(self) -> None:
        self.assertIsNone(overall_restore_percent({}, {}))  # nothing yet
        self.assertIsNone(overall_restore_percent({"drive-scsi0": 50}, {}))  # sizes not read yet
        # a reported disk missing from the size map → fall back (no wrong number)
        self.assertIsNone(overall_restore_percent({"drive-scsi9": 50}, {"drive-scsi0": G}))


if __name__ == "__main__":
    unittest.main()
