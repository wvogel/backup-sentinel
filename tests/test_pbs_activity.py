import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

from app.i18n import DEFAULT_LANGUAGE, set_language
from app.services import pbs_activity
from app.web import activity

NOW = datetime(2026, 11, 26, 3, 24, tzinfo=UTC)
CONN = {"id": 7, "name": "pbs-a"}


def task(kind="verify", worker_type="verificationjob", worker_id="store-a:v-daily", **extra):
    store, _, target = worker_id.partition(":")
    return {
        "upid": f"UPID:{worker_id}",
        "kind": kind,
        "worker_type": worker_type,
        "worker_id": worker_id,
        "store": store,
        "target": target,
        "started": NOW - timedelta(minutes=84),
    } | extra


class RefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        pbs_activity.clear()
        self.addCleanup(pbs_activity.clear)

    def test_nothing_is_asked_without_a_poll_and_a_pbs_only_every_30_s(self) -> None:
        with (
            mock.patch.object(pbs_activity, "fetch_running_maintenance", return_value=[task()]) as fetch,
            mock.patch.object(pbs_activity, "maintenance_progress", return_value={"percent": 42.9, "finished": False}),
            mock.patch.object(pbs_activity, "last_run_seconds", return_value=7800) as last,
        ):
            self.assertEqual(pbs_activity.tasks(7), [])  # rendering a page asks no PBS
            fetch.assert_not_called()
            pbs_activity.refresh([CONN])
            pbs_activity.refresh([CONN])  # a second open page right after
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(last.call_count, 1)
        found = pbs_activity.tasks(7)
        self.assertEqual(found[0]["progress"]["percent"], 42.9)
        self.assertEqual(found[0]["last_seconds"], 7800)

    def test_state_ages_out_when_no_page_polls_any_more(self) -> None:
        with (
            mock.patch.object(pbs_activity, "fetch_running_maintenance", return_value=[task()]),
            mock.patch.object(pbs_activity, "maintenance_progress", return_value={}),
            mock.patch.object(pbs_activity, "last_run_seconds", return_value=None),
        ):
            pbs_activity.refresh([CONN])
        self.assertTrue(pbs_activity.tasks(7))
        self.assertEqual(pbs_activity.tasks(7, max_age=0), [])

    def test_unreachable_pbs_is_left_alone_for_a_while(self) -> None:
        with mock.patch.object(pbs_activity, "fetch_running_maintenance", side_effect=OSError("down")) as fetch:
            pbs_activity.refresh([CONN])
            with mock.patch.object(pbs_activity.time, "monotonic", return_value=pbs_activity.time.monotonic() + 60):
                pbs_activity.refresh([CONN])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(pbs_activity.tasks(7), [])

    def test_finished_task_and_failed_log_read(self) -> None:
        tasks = [task(), task(kind="gc", worker_type="garbage_collection", worker_id="store-a")]

        def progress(conn, upid, kind):
            if kind == "gc":
                raise OSError("log unreadable")
            return {"finished": True}

        with (
            mock.patch.object(pbs_activity, "fetch_running_maintenance", return_value=tasks),
            mock.patch.object(pbs_activity, "maintenance_progress", side_effect=progress),
            mock.patch.object(pbs_activity, "last_run_seconds", return_value=None),
        ):
            pbs_activity.refresh([CONN])
        self.assertEqual([t["kind"] for t in pbs_activity.tasks(7)], ["gc"])  # still shown, without figures


class ViewTests(unittest.TestCase):
    def setUp(self) -> None:
        set_language("en")
        self.addCleanup(set_language, DEFAULT_LANGUAGE)

    def view(self, tasks):
        with mock.patch.object(pbs_activity, "tasks", return_value=tasks):
            return activity.view(7, NOW)

    def test_nothing_running(self) -> None:
        self.assertIsNone(self.view([]))
        self.assertIsNone(activity.view(None))

    def test_gc_and_verify_at_once(self) -> None:
        view = self.view(
            [
                task(
                    progress={"percent": 42.9, "done": 5, "total": 12, "unit": "groups", "group": "vm/104"},
                    last_seconds=7800,
                ),
                task(
                    kind="gc",
                    worker_type="garbage_collection",
                    worker_id="store-a",
                    progress={"phase": 1, "percent": 18.0, "done": 2160, "total": 12000},
                    last_seconds=None,
                ),
                task(worker_type="verify_snapshot", worker_id="store-b:vm/100/2026-10-16T20:00:01Z", progress={}),
            ]
        )
        self.assertEqual(view["kinds"], ["gc", "verify"])
        # Cut off, not rounded.
        self.assertEqual(view["stores"]["store-a"]["kinds"], {"verify": 42, "gc": 18})
        self.assertEqual(view["stores"]["store-b"]["kinds"], {"verify": None})
        lines = view["stores"]["store-a"]["tooltip"].split("\n")
        self.assertTrue(lines[0].startswith("Verify job v-daily · running since "))
        self.assertIn("(1 h 24 min)", lines[0])
        self.assertEqual(lines[1], " • 42 % · 5 of 12 groups")
        self.assertEqual(lines[2], " • now: vm/104")
        self.assertIn("expected to finish around", lines[3])
        self.assertEqual(lines[4], " • last run took 2 h 10 min")
        self.assertEqual(lines[6], " • Phase 1 (mark used chunks) · 18 % · 2,160 of 12,000 index files")
        self.assertIn("Verify job v-daily · store-a · ", view["tooltip"])
        self.assertIn("no progress reported yet", view["stores"]["store-b"]["tooltip"])

    def test_eta_only_with_enough_to_go_on(self) -> None:
        start = NOW - timedelta(minutes=60)
        self.assertEqual(activity.verify_eta(start, 50.0, NOW), NOW + timedelta(minutes=60))
        self.assertIsNone(activity.verify_eta(start, 1.0, NOW))
        self.assertIsNone(activity.verify_eta(start, 100.0, NOW))
        self.assertIsNone(activity.verify_eta(NOW - timedelta(seconds=30), 50.0, NOW))
        self.assertIsNone(activity.verify_eta(None, 50.0, NOW))
