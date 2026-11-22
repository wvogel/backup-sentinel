"""Synthetic but realistic data for the NIS2 report tests and previews: four
clusters, daily backups at ~21:00 with drift, a few real gaps, restore tests."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from typing import Any

CLUSTERS = [("pve-berlin", 29), ("pve-hamburg", 8), ("pve-dresden", 52), ("pve-kiel", 22)]
FIRST_SEEN = datetime(2026, 3, 1, tzinfo=UTC)


class Nis2Fixture:
    def __init__(self, seed: int = 7, until: datetime = datetime(2026, 11, 1, 6, tzinfo=UTC)) -> None:
        rnd = random.Random(seed)
        self.guests: list[dict[str, Any]] = []
        self.backups: dict[int, list[datetime]] = {}
        self.tests: dict[int, list[date]] = {}
        self.protocol: list[dict[str, Any]] = []
        vid = 0
        for ci, (cname, count) in enumerate(CLUSTERS, 1):
            for n in range(count):
                vid += 1
                kind = "weekly" if n % 13 == 12 else ("unconfigured" if (cname, n) == ("pve-dresden", 50) else "daily")
                self.guests.append(
                    {
                        "id": vid,
                        "cluster_id": ci,
                        "cluster_name": cname,
                        "cluster_slug": cname,
                        "vmid": 100 + n,
                        "vm_name": f"srv-{cname.split('-')[1]}-{n:02d}",
                        "node_name": f"node{n % 3 + 1}",
                        "guest_type": "qemu",
                        "first_seen_at": FIRST_SEEN if n < count - 1 else datetime(2026, 10, 14, 9, tzinfo=UTC),
                        "missing_since": None,
                        "backup_kind": kind,
                        "effective_kind": kind,
                        "backup_policy_override": None,
                        "rpo_hours": 24,
                        "restore_required": n % 9 != 8,
                        "restore_cadence": "half_year",
                    }
                )
                if kind == "unconfigured":
                    continue
                times, d = [], datetime(2026, 2, 20, 21, tzinfo=UTC)
                step = timedelta(days=7 if kind == "weekly" else 1)
                while d < until:
                    times.append(d + timedelta(minutes=rnd.randint(0, 40)))
                    d += step
                self.backups[vid] = times
                # last test: some valid, some overdue, a few never
                r = rnd.random()
                if r < 0.8 or cname == "pve-hamburg":
                    self.tests[vid] = [date(2026, 10, 1) - timedelta(days=rnd.randint(1, 150))]
                elif r < 0.93:
                    self.tests[vid] = [date(2026, 3, 1)]
        # Gaps: a datastore-full night in pve-dresden, a node maintenance in pve-kiel.
        for vid in (60, 63):
            self.backups[vid] = [
                t for t in self.backups[vid] if not (date(2026, 10, 11) < t.date() < date(2026, 10, 14))
            ]
        self.backups[96] = [t for t in self.backups[96] if t.date() != date(2026, 10, 26)]
        self.backups[30] = [t for t in self.backups[30] if not (date(2026, 10, 27) < t.date() < date(2026, 10, 31))]
        # Restore runs in October.
        for i, vid in enumerate((1, 12, 31, 45, 45, 62, 80, 99)):
            at = datetime(2026, 10, 2 + i * 3, 14, 23, tzinfo=UTC)
            result = "failed" if i == 3 else ("limited" if i == 6 else "passed")
            self.protocol.append(
                {
                    "kind": "run",
                    "id": i + 1,
                    "vm_id": vid,
                    "trigger": "scheduled",
                    "result": result,
                    "at": at,
                    "started_at": at - timedelta(minutes=40),
                    "duration_seconds": 0 if result == "failed" else 600 + i * 900,
                    "source_pbs": "pbs02",
                    "source_datastore": "ds-main",
                    "source_namespace": "",
                    "snapshot_id": f"pbs02:backup/vm/{100 + vid}/{(at - timedelta(days=1)).strftime('%Y-%m-%dT21:00:00Z')}",
                    "checks": {"10.1.1.5": {"icmp": i % 2 == 0, "arp": True, "ports": {"443": True, "3389": i != 6}}}
                    if result != "failed"
                    else {"error": "guest did not answer within the probe window (no ICMP, no ARP)"},
                    "log": "",
                    "recovery_type": None,
                    "evidence_note": None,
                }
            )
            if result != "failed":
                self.tests.setdefault(vid, []).append(at.date())
        self.protocol.append(
            {
                "kind": "manual",
                "id": 99,
                "vm_id": 9,
                "trigger": "manual",
                "result": "passed",
                "at": datetime(2026, 10, 20, 10, tzinfo=UTC),
                "started_at": None,
                "duration_seconds": 3600,
                "source_pbs": None,
                "source_datastore": None,
                "source_namespace": None,
                "snapshot_id": None,
                "checks": None,
                "log": None,
                "recovery_type": "Datei-Restore",
                "evidence_note": "Ticket 4711",
            }
        )
        self.tests.setdefault(9, []).append(date(2026, 10, 20))

    # The db functions nis2_report calls ------------------------------------
    def nis2_guest_rows(self):
        return [dict(g) for g in self.guests]

    def nis2_backup_times(self, since):
        out = {}
        for vid, times in self.backups.items():
            before = [t for t in times if t < since]
            out[vid] = ([before[-1]] if before else []) + [t for t in times if t >= since]
        return out

    def nis2_backup_counts(self, start, end):
        return {vid: (sum(1 for t in times if start <= t < end), 0) for vid, times in self.backups.items()}

    def nis2_restore_test_dates(self):
        return {k: list(v) for k, v in self.tests.items()}

    def nis2_restore_protocol(self, start, end, start_date, end_date):
        return [p for p in self.protocol if start <= p["at"] < end]

    def nis2_earliest_event_by_cluster(self):
        return {ci: datetime(2026, 2, 20, 21, tzinfo=UTC) for ci in range(1, len(CLUSTERS) + 1)}

    def patch(self):
        from unittest.mock import patch

        names = [n for n in dir(self) if n.startswith("nis2_")]
        patches = [patch(f"app.services.nis2_report.db.{n}", getattr(self, n)) for n in names]

        class _Multi:
            def __enter__(s):
                for p in patches:
                    p.start()
                return self

            def __exit__(s, *a):
                for p in patches:
                    p.stop()

        return _Multi()
