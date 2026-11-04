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
    def test_size_weighted_across_disks(self) -> None:
        total = 200 * G
        size = {"drive-scsi0": 120 * G, "drive-scsi1": 80 * G}
        # disk0 done, disk1 half → (120 + 40) / 200 = 80%.
        out = overall_restore_percent(total, {"drive-scsi0": 100, "drive-scsi1": 50}, size)
        self.assertEqual(out["percent"], 80)
        self.assertEqual(out["bytes_done"], 160 * G)
        self.assertEqual(out["bytes_total"], total)
        self.assertEqual(out["phase"], "restoring")

    def test_monotonic_when_next_disk_enters_at_zero(self) -> None:
        total, size = 200 * G, {"drive-scsi0": 120 * G, "drive-scsi1": 80 * G}
        a = overall_restore_percent(total, {"drive-scsi0": 100}, size)["percent"]  # 60%
        b = overall_restore_percent(total, {"drive-scsi0": 100, "drive-scsi1": 0}, size)["percent"]
        self.assertEqual(a, 60)
        self.assertGreaterEqual(b, a)  # a new disk at 0% must not drop the bar

    def test_all_done_is_finalizing(self) -> None:
        total, size = 200 * G, {"drive-scsi0": 120 * G, "drive-scsi1": 80 * G}
        out = overall_restore_percent(total, {"drive-scsi0": 100, "drive-scsi1": 100}, size)
        self.assertEqual(out["percent"], 100)
        self.assertEqual(out["phase"], "finalizing")

    def test_fallback_when_total_or_size_unknown(self) -> None:
        self.assertIsNone(overall_restore_percent(None, {"drive-scsi0": 50}, {"drive-scsi0": G}))
        self.assertIsNone(overall_restore_percent(0, {"drive-scsi0": 50}, {"drive-scsi0": G}))
        self.assertIsNone(overall_restore_percent(G, {}, {}))  # nothing seen yet
        # a seen drive whose size we don't know yet → fall back (no wrong number)
        self.assertIsNone(overall_restore_percent(G, {"drive-scsi0": 50}, {}))


if __name__ == "__main__":
    unittest.main()
