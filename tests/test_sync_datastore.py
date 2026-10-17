import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

from app.web import sync

CLUSTER = {"name": "pve-x"}
CONN = {"id": 7, "name": "pbs1"}


def _row(store, used, full_in_days=None):
    full = datetime.now(UTC) + timedelta(days=full_in_days) if full_in_days else None
    return {
        "store": store,
        "total_bytes": 100,
        "used_bytes": used,
        "avail_bytes": 100 - used,
        "estimated_full_at": full,
    }


class RecordDatastoreUsageTests(unittest.TestCase):
    def run_with(self, rows=None, error=None):
        log: list[str] = []
        with (
            mock.patch.object(sync, "fetch_datastore_usage", side_effect=error, return_value=rows),
            mock.patch.object(sync.db, "replace_datastore_usage") as replace,
            mock.patch.object(sync, "notify_datastore_full") as notify,
        ):
            sync.record_datastore_usage(CLUSTER, CONN, log)
        return replace, notify, log

    def test_stores_usage_and_alerts_only_critical_stores(self):
        rows = [_row("ok-store", 50), _row("warn-store", 82), _row("full-store", 93), _row("soon", 30, full_in_days=5)]
        replace, notify, log = self.run_with(rows)
        replace.assert_called_once_with(7, rows)
        self.assertEqual(sorted(call.args[2] for call in notify.call_args_list), ["full-store", "soon"])
        self.assertEqual(len(log), 4)

    def test_fetch_error_is_logged_and_nothing_stored(self):
        replace, notify, log = self.run_with(error=RuntimeError("PBS down"))
        replace.assert_not_called()
        notify.assert_not_called()
        self.assertEqual(len(log), 1)


class NotifyFailedVerifiesTests(unittest.TestCase):
    def test_only_critical_verify_rows_alert(self):
        failed_at = datetime.now(UTC)
        rows = [
            {"vm_name": "a", "vmid": 1, "verify_status": "critical", "verify_failed_at": failed_at},
            {"vm_name": "b", "vmid": 2, "verify_status": "warning", "verify_failed_at": None},
            {"vm_name": "c", "vmid": 3, "verify_status": None, "verify_failed_at": None},
        ]
        with (
            mock.patch.object(sync.db, "vm_governance_rows", return_value=rows),
            mock.patch.object(sync, "notify_verify_failed") as notify,
        ):
            sync.notify_failed_verifies({"id": 1, "name": "pve-x"})
        notify.assert_called_once_with("pve-x", "a", 1, failed_at)


class StepTimesTests(unittest.TestCase):
    def test_slowest_step_first_and_repeated_steps_add_up(self) -> None:
        step = sync._StepTimes()
        clock = iter([0.0, 1.0, 1.0, 5.0, 5.0, 5.5])
        with mock.patch.object(sync.time, "monotonic", lambda: next(clock)):
            with step("DB"):
                pass
            with step("Task-Logs"):
                pass
            with step("DB"):
                pass
        self.assertEqual(step.line(), sync.ts("synclog.timings", steps="Task-Logs 4.0s · DB 1.5s"))


class NotifyGuestAlertsTests(unittest.TestCase):
    def row(self, vmid, restore, age=None, interval=90, gone=False):
        return {
            "vm_name": f"vm{vmid}",
            "vmid": vmid,
            "backup_severity": "ok",
            "status_backup_kind": "daily",
            "restore_due_status": restore,
            "restore_age_days": age,
            "restore_interval_days": interval,
            "vm_gone": gone,
        }

    def test_only_expired_restore_tests_of_present_guests_alert(self):
        rows = [
            self.row(1, "critical", age=100),
            self.row(2, "open"),  # never tested: not overdue
            self.row(3, "warning", age=80),
            self.row(4, "critical", age=200, gone=True),
        ]
        with mock.patch.object(sync, "notify_restore_overdue") as notify:
            sync.notify_guest_alerts(CLUSTER, rows)
        notify.assert_called_once_with("pve-x", "vm1", 1, 10)


class RestoreAlertSettingTests(unittest.TestCase):
    def send(self, settings):
        from app.services import notifications

        with (
            mock.patch.object(notifications, "_notify_settings", return_value=settings),
            mock.patch.object(notifications, "_should_send", return_value=True),
            mock.patch.object(notifications, "send_gotify") as gotify,
        ):
            notifications.notify_restore_overdue("pve-x", "vm1", 1, 10)
        return gotify.called

    def test_off_by_default(self):
        self.assertFalse(self.send({}))

    def test_sent_when_switched_on(self):
        self.assertTrue(self.send({"notify_restore_overdue": "true"}))


if __name__ == "__main__":
    unittest.main()
