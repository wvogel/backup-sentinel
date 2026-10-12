import unittest
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.services.vzdump_log import guest_name_from_notes, parse_vzdump_log

BERLIN = ZoneInfo("Europe/Berlin")

# Excerpt of a real multi-VM job: VM 100 incremental, VM 103 full (bitmap invalid).
JOB_LOG = [
    "INFO: starting new backup job: vzdump --all 1 --storage pbs-fh-muc --mode snapshot",
    "INFO: Starting Backup of VM 100 (qemu)",
    "INFO: Backup started at 2026-10-09 20:00:01",
    "INFO: creating Proxmox Backup Server archive 'vm/100/2026-10-09T18:00:01Z'",
    "INFO: scsi0: dirty-bitmap status: OK (12.8 GiB of 120.0 GiB dirty)",
    "INFO: using fast incremental mode (dirty-bitmap), 12.8 GiB dirty of 120.0 GiB total",
    "INFO: 100% (12.8 GiB of 12.8 GiB) in 1m 33s, read: 84.0 MiB/s, write: 84.0 MiB/s",
    "INFO: backup is sparse: 4.00 MiB (0%) total zero data",
    "INFO: backup was done incrementally, reused 108.25 GiB (90%)",
    "INFO: transferred 12.77 GiB in 300 seconds (43.6 MiB/s)",
    "INFO: Finished Backup of VM 100 (00:05:05)",
    "INFO: Backup finished at 2026-10-09 20:05:06",
    "INFO: Starting Backup of VM 103 (qemu)",
    "INFO: Backup started at 2026-10-09 20:05:06",
    "INFO: creating Proxmox Backup Server archive 'vm/103/2026-10-09T18:05:06Z'",
    "INFO: scsi0: dirty-bitmap status: existing bitmap was invalid and has been cleared",
    "INFO: transferred 37.30 TiB in 185000 seconds (211.4 MiB/s)",
    "INFO: Finished Backup of VM 103 (51:23:20)",
]


class ParseVzdumpLogTests(unittest.TestCase):
    def test_incremental_run_details(self) -> None:
        run = parse_vzdump_log(JOB_LOG, BERLIN)[0]
        self.assertEqual(run.vmid, 100)
        self.assertEqual(run.started_at, datetime(2026, 10, 9, 18, 0, 1, tzinfo=UTC))
        self.assertEqual(run.duration_seconds, 5 * 60 + 5)
        self.assertEqual(run.transferred_bytes, int(12.77 * 1024**3))
        self.assertEqual(run.throughput_bps, int(43.6 * 1024**2))
        self.assertEqual(run.reused_pct, 90)
        self.assertIs(run.full_backup, False)
        self.assertFalse(run.failed)

    def test_invalid_bitmap_means_full_run(self) -> None:
        run = parse_vzdump_log(JOB_LOG, BERLIN)[1]
        self.assertEqual(run.vmid, 103)
        self.assertIs(run.full_backup, True)
        self.assertIsNone(run.reused_pct)
        self.assertEqual(run.duration_seconds, 51 * 3600 + 23 * 60 + 20)

    def test_tpm_and_efi_bitmaps_do_not_make_a_full_run(self) -> None:
        # Real log (S-APP04): the TPM state is attached for the backup only, so its
        # bitmap is always "created new" — the data disk still ran incrementally.
        runs = parse_vzdump_log(
            [
                "INFO: Starting Backup of VM 21434 (qemu)",
                "INFO: creating Proxmox Backup Server archive 'vm/21434/2026-10-11T16:16:40Z'",
                "INFO: efidisk0: dirty-bitmap status: OK (drive clean)",
                "INFO: scsi0: dirty-bitmap status: OK (11.2 GiB of 120.0 GiB dirty)",
                "INFO: tpmstate0-backup: dirty-bitmap status: created new",
                "INFO: using fast incremental mode (dirty-bitmap), 11.2 GiB dirty of 120.0 GiB total",
                "INFO: backup was done incrementally, reused 109.76 GiB (91%)",
                "INFO: transferred 11.21 GiB in 61 seconds (188.3 MiB/s)",
                "INFO: Finished Backup of VM 21434 (00:01:03)",
            ],
            BERLIN,
        )
        self.assertIs(runs[0].full_backup, False)

    def test_unusable_bitmap_of_any_data_disk_means_full_run(self) -> None:
        runs = parse_vzdump_log(
            [
                "INFO: Starting Backup of VM 21433 (qemu)",
                "INFO: scsi0: dirty-bitmap status: OK (7.8 GiB of 120.0 GiB dirty)",
                "INFO: scsi1: dirty-bitmap status: created new",
                "INFO: tpmstate0-backup: dirty-bitmap status: created new",
                "INFO: backup was done incrementally, reused 193.23 GiB (45%)",
                "INFO: Finished Backup of VM 21433 (00:20:45)",
            ],
            BERLIN,
        )
        self.assertIs(runs[0].full_backup, True)

    def test_failed_vm(self) -> None:
        runs = parse_vzdump_log(
            [
                "INFO: Starting Backup of VM 105 (qemu)",
                "INFO: Backup started at 2026-10-09 21:00:00",
                "ERROR: Backup of VM 105 failed - unable to connect to PBS",
            ],
            BERLIN,
        )
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0].failed)
        self.assertIsNone(runs[0].full_backup)

    def test_file_backup_uses_node_local_start_time(self) -> None:
        runs = parse_vzdump_log(
            [
                "INFO: Starting Backup of VM 200 (qemu)",
                "INFO: Backup started at 2026-10-09 22:15:00",
                "INFO: creating vzdump archive '/mnt/pve/nfs/dump/vzdump-qemu-200-2026_10_09-22_15_00.vma.zst'",
                "INFO: transferred 32.00 GiB in 128 seconds",
                "INFO: Finished Backup of VM 200 (00:02:10)",
            ],
            BERLIN,
        )
        run = runs[0]
        self.assertEqual(run.started_at, datetime(2026, 10, 9, 20, 15, tzinfo=UTC))
        self.assertEqual(run.throughput_bps, 32 * 1024**3 // 128)
        self.assertIsNone(run.full_backup)


class GuestNameTests(unittest.TestCase):
    def test_first_line_of_notes(self) -> None:
        self.assertEqual(guest_name_from_notes("zeiss-fh-muc\nextra"), "zeiss-fh-muc")
        self.assertIsNone(guest_name_from_notes(""))
        self.assertIsNone(guest_name_from_notes(None))


if __name__ == "__main__":
    unittest.main()
