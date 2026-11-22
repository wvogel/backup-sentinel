import json
import sys
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from nis2_fixture import Nis2Fixture

from app.services import nis2_render, nis2_report
from app.services.reporting import serialize_report_payload

OCT = date(2026, 10, 1)
AFTER_OCT = datetime(2026, 11, 1, 6, tzinfo=UTC)


def build(lang="de", **kw):
    fx = Nis2Fixture()
    with fx.patch():
        return nis2_report.build_nis2_report(OCT, trigger="auto", lang=lang, now=kw.pop("now", AFTER_OCT), **kw)


class BuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = build()
        cls.by = {c["slug"]: c for c in cls.r["clusters"]}

    def test_full_month_period(self):
        p = self.r["period"]
        self.assertFalse(p["interim"])
        self.assertEqual(len(p["days"]), 31)
        self.assertEqual(p["reference_date"], date(2026, 10, 31))

    def test_cluster_without_gaps_is_100(self):
        self.assertEqual(self.by["pve-berlin"]["gap_count"], 0)
        self.assertEqual(self.by["pve-berlin"]["rpo_pct"], 100)

    def test_three_days_without_backup_is_one_gap_and_truncated(self):
        fh = self.by["pve-hamburg"]
        self.assertEqual(fh["gap_count"], 1)
        self.assertEqual(fh["rpo_pct"], 98)  # 98.x truncated, never 99
        self.assertEqual(fh["restore_pct"], 100)

    def test_guest_without_backup_job_counts_as_uncovered(self):
        gaps = self.by["pve-dresden"]["gaps"]
        never = [g for g in gaps if g["prev_backup"] is None]
        self.assertEqual(len(never), 1)
        self.assertTrue(never[0]["open"])
        self.assertEqual(self.by["pve-dresden"]["open_gaps"], 1)
        self.assertEqual(self.by["pve-dresden"]["verdict"], "critical")

    def test_weekly_guest_gap_within_180h_is_no_gap(self):
        names = {g["vm_name"] for g in self.by["pve-dresden"]["gaps"]}
        self.assertNotIn("srv-mar-25", names)  # weekly: 3 days off is within 180 h
        self.assertIn("srv-mar-22", names)

    def test_failed_attempt_superseded_by_later_pass_is_kept_but_not_counted(self):
        mar = self.by["pve-dresden"]
        failed = next(p for p in mar["protocol"] if p["result"] == "failed")
        self.assertNotIn("note", failed)  # no failure details in the report
        self.assertEqual(failed["superseded_by"], "14.10. 16:23")  # srv-mar-07 passed three days later
        self.assertNotIn(failed, mar["protocol_counted"])
        self.assertEqual((mar["tests_passed"], mar["tests_run"]), (3, 3))
        self.assertEqual(len(mar["protocol"]), 4)  # still on record in the appendix

    def test_supersede_rules(self):
        rows = [
            {"vm_id": 1, "result": "passed", "at_label": "a"},
            {"vm_id": 1, "result": "failed", "at_label": "b"},  # pass was BEFORE: counts
            {"vm_id": 2, "result": "limited", "at_label": "c"},
            {"vm_id": 3, "result": "passed", "at_label": "d"},  # other VM: no effect on 2
            {"vm_id": 2, "result": "passed", "at_label": "e"},
            {"vm_id": 4, "result": "error", "at_label": "f"},
            {"vm_id": 4, "result": "limited", "at_label": "g"},  # limited is no full pass
        ]
        nis2_report._mark_superseded(rows)
        self.assertEqual([r["superseded_by"] for r in rows], [None, None, "e", None, None, None, None])

    def test_documented_test_without_run_is_in_protocol(self):
        manual = [p for p in self.by["pve-berlin"]["protocol"] if p["kind"] == "manual"]
        self.assertEqual(len(manual), 1)
        self.assertEqual(manual[0]["checks"], [{"label": "Datei-Restore", "ok": None}])
        self.assertEqual(manual[0]["snapshot"], "dokumentiert")

    def test_every_check_listed_with_its_result_ports_in_order(self):
        limited = next(p for p in self.by["pve-dresden"]["protocol"] if p["result"] == "limited")
        self.assertEqual(
            limited["checks"],
            [
                {"label": "ICMP", "ok": True},
                {"label": "ARP", "ok": True},
                {"label": "TCP 443", "ok": True},
                {"label": "TCP 3389", "ok": False},
            ],
        )

    def test_snapshot_shows_only_its_date(self):
        run = self.by["pve-kiel"]["protocol"][0]
        self.assertEqual(run["snapshot"], "22.10. 23:00")

    def test_trend_has_holes_before_data_never_zero(self):
        trend = self.r["overall"]["trend"]["rpo"]
        self.assertEqual(len(trend), 12)
        self.assertIsNone(trend[0])  # Nov 2025: no data yet
        self.assertNotIn(0, trend)
        self.assertEqual(trend[-1], self.r["overall"]["rpo_pct"])

    def test_overall_sums_clusters(self):
        o = self.r["overall"]
        self.assertEqual(o["vms"], sum(c["vms"] for c in self.r["clusters"]))
        self.assertEqual(o["gap_count"], sum(c["gap_count"] for c in self.r["clusters"]))
        self.assertEqual(o["clusters"], 4)

    def test_day_strip_marks_gap_days(self):
        fh = self.by["pve-hamburg"]
        self.assertEqual(fh["day_tones"][0], "ok")
        self.assertIn("critical", fh["day_tones"][27:31])

    def test_summary_states_only_facts(self):
        text = " ".join(self.by["pve-berlin"]["summary"])
        self.assertIn("Keine Lücke über RPO", text)
        text = " ".join(self.by["pve-dresden"]["summary"])
        self.assertIn("davon 1 noch offen", text)

    def test_serialisable(self):
        json.dumps(serialize_report_payload(self.r))

    def test_cluster_view(self):
        view = nis2_report.cluster_view(self.r, "pve-kiel")
        self.assertIsNone(view["overall"])
        self.assertEqual([c["slug"] for c in view["clusters"]], ["pve-kiel"])
        self.assertIsNone(nis2_report.cluster_view(self.r, "nope"))


class InterimTest(unittest.TestCase):
    def test_running_month_is_cut_at_now(self):
        now = datetime(2026, 10, 15, 12, tzinfo=UTC)
        r = build(now=now)
        self.assertTrue(r["period"]["interim"])
        self.assertEqual(r["period"]["end"], now)
        self.assertEqual(len(r["period"]["days"]), 15)
        self.assertEqual(r["period"]["reference_date"], date(2026, 10, 15))


class RenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = build()

    def test_html_has_every_section_and_no_raw_keys(self):
        for lang in ("de", "en"):
            html = nis2_render.render_html(build(lang=lang))
            self.assertNotIn("nis2.", html)
            self.assertEqual(html.count('class="exec"'), 5)
            self.assertEqual(html.count('class="appendix'), 4)

    def test_percent_never_rounded_up_in_output(self):
        html = nis2_render.render_html(self.r)
        self.assertIn(">98 %<", html.replace("</b>", "<").replace("<b>", ">"))

    def test_each_management_page_fits_on_one_page(self):
        from weasyprint import HTML

        doc = HTML(string=nis2_render.render_html(self.r), base_url=str(nis2_render.TEMPLATE_DIR)).render()
        texts = []
        for page in doc.pages:
            words = []

            def walk(box, words=words):
                if hasattr(box, "text"):
                    words.append(box.text)
                for child in getattr(box, "children", []) or []:
                    walk(child)

            walk(page._page_box)
            texts.append(" ".join(words))
        # overall + 4 cluster pages, then the appendix starts on page 6
        self.assertIn("Nachweis Datensicherung", texts[0])
        for i, name in enumerate(["pve-berlin", "pve-hamburg", "pve-dresden", "pve-kiel"], 1):
            self.assertIn(name, texts[i])
        self.assertIn("Anhang ·", texts[5])
        self.assertNotIn("Anhang ·", " ".join(texts[:5]))

    def test_cluster_pdf_has_signoff(self):
        html = nis2_render.render_html(nis2_report.cluster_view(self.r, "pve-kiel"))
        self.assertIn("Geschäftsführung", html)
        self.assertEqual(html.count('class="exec"'), 1)


if __name__ == "__main__":
    unittest.main()
