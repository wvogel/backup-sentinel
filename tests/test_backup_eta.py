"""ETA of running full-read backups: empty space vs. real data."""

import unittest
from datetime import UTC, datetime
from pathlib import Path

from app.services import proxmox_progress as pp

GIB = 1024**3
TIB = 1024**4
# Real log (VM 144, 3 disks, 3.1 TiB, all dirty bitmaps invalid) up to 60 %:
# empty stretches at 6–24 % and 28–31 % pass at ~9 GiB/s, data at ~180 MiB/s.
SPARSE_LOG = (Path(__file__).parent / "fixtures" / "vzdump_full_read_sparse.log").read_text().splitlines()
START = datetime(2026, 11, 16, 3, 33, 38, tzinfo=UTC)
AT_60 = 7420  # elapsed seconds of the last line (2h 3m 40s)


def eta(lines, allocated=None) -> datetime:
    return pp._parse_progress_data({}, list(lines), allocated)["eta"]


def line(pct: int, written: str, total: str, elapsed: str, rate: str = "100.0 MiB/s") -> str:
    return f"INFO: {pct:3d}% ({written} of {total}) in {elapsed}, read: {rate}, write: {rate}"


class ProgressSamplesTests(unittest.TestCase):
    def test_coarse_tib_labels_fall_back_to_the_percentage(self) -> None:
        # "1.0 TiB" stands for anything from 0.95 to 1.05 TiB — coarser than 1 % of 3.1 TiB.
        samples = pp._progress_samples(SPARSE_LOG)
        self.assertEqual(samples[-1][0], AT_60)
        self.assertEqual(samples[-1][1], samples[-1][2] * 60 // 100)

    def test_fine_labels_are_kept(self) -> None:
        samples = pp._progress_samples([line(5, "161.1 GiB", "3.1 TiB", "16m 51s")])
        self.assertEqual(samples, [(1011, int(161.1 * GIB), int(3.1 * TIB))])


class EstimateTests(unittest.TestCase):
    def test_empty_stretches_do_not_shorten_the_data_rate(self) -> None:
        # 100 GiB data in 1000 s, then 900 GiB empty in 10 s, then 100 GiB data
        # in 1000 s; ~1 TiB left that is all data → ~10000 s at 100 GiB/1000 s.
        # (The 10 s empty stretch counts as 1 GiB data read: at most what the
        # data rate allows in that time.) The plain average would say ~1900 s.
        lines = [
            line(5, "100.0 GiB", "2100.0 GiB", "16m 40s"),
            line(47, "1000.0 GiB", "2100.0 GiB", "16m 50s"),
            line(52, "1100.0 GiB", "2100.0 GiB", "33m 30s"),
        ]
        remaining = pp._estimate_remaining_seconds(pp._progress_samples(lines), allocated_bytes=1200 * GIB)
        self.assertAlmostEqual(remaining, 9990, delta=20)

    def test_without_allocated_size_the_rest_keeps_the_mix_seen_so_far(self) -> None:
        # Real log: ~62 % of what was read so far was data; the rest is assumed
        # alike, read at the current data rate.
        self.assertEqual(eta(SPARSE_LOG), datetime(2026, 11, 16, 6, 49, 54, tzinfo=UTC))

    def test_allocated_size_tells_how_much_data_is_left(self) -> None:
        little, much = eta(SPARSE_LOG, int(1.6 * TIB)), eta(SPARSE_LOG, int(2.4 * TIB))
        self.assertLess(little, eta(SPARSE_LOG))
        self.assertLess(little, much)
        # All of the rest is data: 40 % of 3.1 TiB at ~185 MiB/s.
        self.assertEqual(much, eta(SPARSE_LOG, 10 * TIB))
        self.assertEqual(much, datetime(2026, 11, 16, 7, 38, 39, tzinfo=UTC))

    def test_too_few_lines_use_the_average_of_the_run(self) -> None:
        lines = [line(10, "10.0 GiB", "100.0 GiB", "10m")]
        self.assertIsNone(pp._estimate_remaining_seconds(pp._progress_samples(lines)))
        progress = pp._parse_progress_data({}, ["INFO: Backup started at 2026-11-16 04:00:00", *lines])
        self.assertEqual((progress["eta"] - progress["started_at"]).total_seconds(), 6000)


class AllocatedSizeTests(unittest.TestCase):
    DISKS = (
        "INFO: include disk 'scsi0' 'fleecing-nfs:144/vm-144-disk-0.qcow2' 150G",
        "INFO: include disk 'scsi1' 'fleecing-nfs:144/vm-144-disk-1.qcow2' 2T",
    )

    def test_needs_disks_progress_and_a_full_read(self) -> None:
        progress = [line(1, "1.0 GiB", "100.0 GiB", "10s")]
        self.assertTrue(pp._needs_allocated([*self.DISKS, *progress]))
        self.assertFalse(pp._needs_allocated(self.DISKS))
        self.assertFalse(pp._needs_allocated(progress))
        dirty = "INFO: using fast incremental mode (dirty-bitmap), 4.0 GiB dirty of 100.0 GiB total"
        self.assertFalse(pp._needs_allocated([*self.DISKS, dirty, *progress]))

    def test_sums_used_space_of_all_disks(self) -> None:
        sizes = {"fleecing-nfs:144/vm-144-disk-0.qcow2": (90, 150), "fleecing-nfs:144/vm-144-disk-1.qcow2": (900, 2048)}
        asked = []

        def get_volume(storage, volid):
            asked.append(storage)
            used, size = sizes[volid]
            return {"used": used * GIB, "size": size * GIB}

        self.assertEqual(pp._allocated_bytes(pp._included_disks(list(self.DISKS)), get_volume), 990 * GIB)
        self.assertEqual(asked, ["fleecing-nfs", "fleecing-nfs"])

    def test_unknown_when_a_storage_reports_nothing(self) -> None:
        # LVM, RBD & co. mostly answer used = 0.
        self.assertIsNone(pp._allocated_bytes(["lvm:vm-1-disk-0"], lambda s, v: {"used": 0, "size": GIB}))

    def test_kept_while_the_vm_runs_and_reset_for_the_next(self) -> None:
        state = pp._TaskLog(10, ["INFO: Starting Backup of VM 144 (qemu)"], True, 5 * GIB)
        same = pp._with_lines(state, ["INFO:  61% ..."], 11)
        self.assertEqual((same.allocated_asked, same.allocated), (True, 5 * GIB))
        nxt = pp._with_lines(same, ["INFO: Starting Backup of VM 145 (qemu)"], 12)
        self.assertEqual((nxt.allocated_asked, nxt.allocated), (False, None))


class DiskBarTests(unittest.TestCase):
    INCLUDES = (
        "INFO: include disk 'scsi0' 'nfs-a:144/vm-144-disk-0.qcow2' 150G",
        "INFO: include disk 'scsi1' 'nfs-a:144/vm-144-disk-1.qcow2' 2T",
        "INFO: include disk 'scsi2' 'nfs-b:144/vm-144-disk-2.qcow2' 1T",
        "INFO: include disk 'efidisk0' 'nfs-a:144/vm-144-disk-3.qcow2' 528K",
    )

    def fills(self, lines, pct):
        return {d["dev"]: d["fill"] for d in pp._disk_bar(lines, pct)}

    def test_full_read_fills_the_disks_one_after_another(self) -> None:
        # 3.15 TiB in all; 30 % ≈ 0.95 TiB: scsi0 (150 GiB) done, scsi1 under half.
        self.assertEqual(self.fills(list(self.INCLUDES), 30), {"scsi0": 100, "scsi1": 40, "scsi2": 0})
        self.assertEqual(self.fills(list(self.INCLUDES), 0), {"scsi0": 0, "scsi1": 0, "scsi2": 0})
        self.assertEqual(self.fills(list(self.INCLUDES), 100), {"scsi0": 100, "scsi1": 100, "scsi2": 100})

    def test_sizes_and_storages_for_the_segments(self) -> None:
        bar = pp._disk_bar(list(self.INCLUDES), 10)
        self.assertEqual([d["dev"] for d in bar], ["scsi0", "scsi1", "scsi2"])  # EFI vars left out
        self.assertEqual(bar[1]["size_bytes"], 2 * TIB)
        self.assertEqual([d["storage"] for d in bar], ["nfs-a", "nfs-a", "nfs-b"])

    def test_incremental_run_weights_by_dirty_data(self) -> None:
        lines = [
            *self.INCLUDES,
            "INFO: scsi0: dirty-bitmap status: OK (10.0 GiB of 150.0 GiB dirty)",
            "INFO: scsi1: dirty-bitmap status: OK (drive clean)",
            "INFO: scsi2: dirty-bitmap status: OK (30.0 GiB of 1.0 TiB dirty)",
        ]
        # 40 GiB to send; 50 % = 20 GiB: scsi0's 10 GiB done, the clean scsi1
        # passed, scsi2 a third through.
        self.assertEqual(self.fills(lines, 50), {"scsi0": 100, "scsi1": 100, "scsi2": 33})
        self.assertEqual(self.fills(lines, 10), {"scsi0": 40, "scsi1": 0, "scsi2": 0})

    def test_no_disks_named_no_bar(self) -> None:
        self.assertIsNone(pp._disk_bar(["INFO:  10% (1.0 GiB of 10.0 GiB) in 10s, read: 1 B/s, write: 1 B/s"], 10))

    def test_progress_carries_the_bar(self) -> None:
        lines = [*self.INCLUDES, line(30, "967.0 GiB", "3.1 TiB", "1h")]
        self.assertEqual([d["fill"] for d in pp._parse_progress_data({}, lines)["disks"]], [100, 40, 0])


if __name__ == "__main__":
    unittest.main()
