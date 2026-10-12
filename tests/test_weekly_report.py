import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from app.services import weekly_report

BERLIN = ZoneInfo("Europe/Berlin")


class DueTests(unittest.TestCase):
    MONDAY_8 = datetime(2026, 10, 12, 8, 0, tzinfo=BERLIN)  # Monday, ISO week 42

    def test_due_on_monday_from_7_am_once_per_week(self):
        self.assertEqual(weekly_report.due_week(self.MONDAY_8, last_sent=""), "2026-W42")
        self.assertIsNone(weekly_report.due_week(self.MONDAY_8, last_sent="2026-W42"))

    def test_not_before_7_am_on_monday(self):
        self.assertIsNone(weekly_report.due_week(self.MONDAY_8.replace(hour=6), last_sent=""))

    def test_catches_up_later_in_the_week(self):
        # Server was down on Monday — send on Wednesday instead of skipping the week.
        self.assertEqual(weekly_report.due_week(self.MONDAY_8 + timedelta(days=2), last_sent="2026-W41"), "2026-W42")


def _row(**kw):
    base = {
        "cluster_name": "pve-x",
        "vm_name": "vm",
        "vm_gone": False,
        "backup_severity": "ok",
        "backup_age_days": 0,
        "restore_due_status": "ok",
        "verify_status": "ok",
        "copy_status": "ok",
        "copy_count": 2,
        "last_restore_tested_at": None,
    }
    return {**base, **kw}


class BuildTests(unittest.TestCase):
    NOW = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)

    def build(self, rows, datastores=(), stats=None):
        with mock.patch.object(weekly_report, "ts", side_effect=lambda key, **kw: key + str(sorted(kw.items()))):
            return weekly_report.build(rows, list(datastores), stats or {"full_runs": [], "failed": 0}, self.NOW)

    def test_lists_problem_vms_and_counts(self):
        subject, body = self.build(
            [
                _row(vm_name="ok-vm"),
                _row(vm_name="crit-vm", backup_severity="critical", backup_age_days=3),
                _row(vm_name="one-copy", copy_status="single", copy_count=1),
                _row(vm_name="bad-verify", verify_status="critical"),
                _row(vm_name="gone", vm_gone=True, backup_severity="critical"),
            ],
            datastores=[
                {"pbs_name": "pbs1", "store": "nas", "pct": 93, "status": "critical", "estimated_full_at": None}
            ],
            stats={"full_runs": ["big-vm"], "failed": 2},
        )
        self.assertIn("weekly.subject", subject)
        self.assertIn("crit-vm", body)
        self.assertIn("one-copy", body)
        self.assertIn("bad-verify", body)
        self.assertIn("big-vm", body)
        self.assertIn("nas", body)
        self.assertNotIn("ok-vm", body)
        self.assertNotIn("'gone'", body)

    def test_all_good_says_so(self):
        _subject, body = self.build([_row(vm_name="ok-vm")])
        self.assertIn("weekly.all_good", body)

    def test_single_backup_location_is_listed_as_info_not_as_problem(self):
        _subject, body = self.build([_row(vm_name="one-copy", copy_status="single", copy_count=1)])
        self.assertIn("weekly.all_good", body)
        self.assertIn("weekly.section_copies", body)
        self.assertIn("one-copy", body)


class HtmlTests(unittest.TestCase):
    NOW = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)

    def render(self, rows, datastores=(), stats=None, app_url="https://bs.example"):
        report = weekly_report.collect(rows, list(datastores), stats or {"full_runs": [], "failed": 0}, self.NOW)
        return weekly_report.render_html(report, primary="#141450", accent="#ffff00", app_url=app_url, version="v9.9.9")

    def test_problems_link_to_their_cluster_and_are_escaped(self):
        html = self.render(
            [
                _row(vm_name="crit<vm>", cluster_slug="pve-x", backup_severity="critical", backup_age_days=3),
                _row(vm_name="one-copy", copy_status="single", copy_count=1),
            ],
            datastores=[
                {"pbs_name": "pbs1", "store": "nas", "pct": 93, "status": "critical", "estimated_full_at": None}
            ],
        )
        self.assertIn("crit&lt;vm&gt;", html)
        self.assertNotIn("crit<vm>", html)
        self.assertIn('href="https://bs.example/clusters/pve-x"', html)
        self.assertIn("width:93%", html)
        self.assertIn("one-copy", html)
        self.assertIn("v9.9.9", html)

    def test_all_good_has_no_problem_sections(self):
        report = weekly_report.collect([_row()], [], {"full_runs": [], "failed": 0}, self.NOW)
        self.assertEqual(report.problems, [])
        html = self.render([_row()], app_url="")
        self.assertIn("✓", html)
        self.assertNotIn("/clusters/", html)  # no app URL configured → no links

    def test_full_runs_are_information_not_problems(self):
        report = weekly_report.collect([_row()], [], {"full_runs": ["pve-x / big-vm"], "failed": 0}, self.NOW)
        self.assertEqual(report.problems, [])
        self.assertEqual([(i.cluster, i.name) for i in report.infos[0].items], [("pve-x", "big-vm")])

    def test_period_is_the_last_seven_days(self):
        report = weekly_report.collect([_row()], [], {"full_runs": [], "failed": 0}, self.NOW)
        self.assertEqual((report.period_start.isoformat(), report.period_end.isoformat()), ("2026-10-05", "2026-10-11"))


class SendIfDueTests(unittest.TestCase):
    def run_with(self, settings, email_enabled=True, sent=True, week="2026-W42"):
        from app.web import sync

        with (
            mock.patch.object(sync.db, "get_settings_dict", return_value=settings),
            mock.patch.object(sync.db, "vm_governance_rows", return_value=[]),
            mock.patch.object(sync.db, "list_datastore_usage", return_value=[]),
            mock.patch.object(sync.db, "weekly_backup_stats", return_value={"failed": 0, "full_runs": []}),
            mock.patch.object(sync.db, "save_settings") as save,
            mock.patch.object(sync.weekly_report, "due_week", return_value=week),
            mock.patch.object(sync, "channel_enabled", return_value=email_enabled),
            mock.patch.object(sync, "send_email", return_value=sent) as send,
        ):
            sync.send_weekly_report_if_due()
        return save, send

    def test_due_and_sent_marks_the_week(self):
        save, send = self.run_with({})
        send.assert_called_once()
        save.assert_called_once_with({"weekly_report_last_week": "2026-W42"})

    def test_send_failure_retries_later(self):
        save, _send = self.run_with({}, sent=False)
        save.assert_not_called()

    def test_email_disabled_marks_the_week_without_sending(self):
        save, send = self.run_with({}, email_enabled=False)
        send.assert_not_called()
        save.assert_called_once_with({"weekly_report_last_week": "2026-W42"})

    def test_switched_off_does_nothing(self):
        save, send = self.run_with({"notify_weekly_report": "false"})
        send.assert_not_called()
        save.assert_not_called()

    def test_not_due_does_nothing(self):
        save, send = self.run_with({}, week=None)
        send.assert_not_called()
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class EmailPartsTests(unittest.TestCase):
    def test_html_is_sent_as_alternative_to_the_text(self):
        from app.services import notifications

        sent = []
        smtp = mock.MagicMock()
        smtp.__enter__.return_value.send_message.side_effect = sent.append
        config = {"email_host": "mail.example", "email_recipients": "ops@example.org", "email_from": "bs@example.org"}
        with (
            mock.patch.object(notifications, "_email_config", return_value=config),
            mock.patch.object(notifications.smtplib, "SMTP_SSL", return_value=smtp),
        ):
            self.assertTrue(notifications._send_email_raw("Subject", "plain text", html="<p>rich</p>"))
        msg = sent[0]
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        self.assertEqual([p.get_content_type() for p in msg.iter_parts()], ["text/plain", "text/html"])
