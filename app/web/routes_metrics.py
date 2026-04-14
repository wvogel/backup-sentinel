from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app import db

router = APIRouter()


def _fmt_metric(name: str, help_text: str, metric_type: str, samples: list[tuple[dict[str, str], float]]) -> str:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}"]
    for labels, value in samples:
        if labels:
            label_str = (
                "{"
                + ",".join(
                    f'{k}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
                    for k, v in labels.items()
                )
                + "}"
            )
        else:
            label_str = ""
        lines.append(f"{name}{label_str} {value}")
    return "\n".join(lines)


@router.get("/metrics", response_class=PlainTextResponse)
def metrics() -> PlainTextResponse:
    output_parts = []
    now = datetime.now(UTC)

    # Cluster sync health
    try:
        clusters = db.list_clusters()
        sync_ok_samples = []
        sync_age_samples = []
        failures_samples = []
        for c in clusters:
            labels = {"cluster": c["name"]}
            sync_ok = c.get("last_pve_sync_ok")
            sync_ok_samples.append((labels, 1 if sync_ok else 0))
            last_sync = c.get("last_pve_sync_at")
            if last_sync:
                age = (now - last_sync).total_seconds()
                sync_age_samples.append((labels, age))
            failures_samples.append((labels, c.get("consecutive_sync_failures", 0) or 0))

        output_parts.append(
            _fmt_metric(
                "backup_sentinel_cluster_sync_ok",
                "Whether last cluster sync succeeded (1=ok, 0=failed or never)",
                "gauge",
                sync_ok_samples,
            )
        )
        output_parts.append(
            _fmt_metric(
                "backup_sentinel_cluster_sync_age_seconds",
                "Seconds since last cluster sync attempt",
                "gauge",
                sync_age_samples,
            )
        )
        output_parts.append(
            _fmt_metric(
                "backup_sentinel_cluster_sync_failures_total",
                "Consecutive sync failures per cluster",
                "counter",
                failures_samples,
            )
        )
    except Exception:
        pass

    # VM backup severity + restore overdue + unencrypted counts
    try:
        clusters = db.list_clusters()
        severity_samples: list[tuple[dict[str, str], float]] = []
        unencrypted_samples: list[tuple[dict[str, str], float]] = []
        restore_overdue_samples: list[tuple[dict[str, str], float]] = []
        for c in clusters:
            rows = db.vm_governance_rows(cluster_id=c["id"])
            severity_count: dict[str, int] = {"ok": 0, "warning": 0, "critical": 0, "unknown": 0}
            restore_overdue = 0
            for r in rows:
                sev = r.get("backup_severity", "unknown")
                severity_count[sev] = severity_count.get(sev, 0) + 1
                if r.get("restore_due_status") in ("warning", "critical"):
                    restore_overdue += 1

            for sev, count in severity_count.items():
                severity_samples.append(({"cluster": c["name"], "severity": sev}, count))

            # Unencrypted VMs via dedicated query (governance rows don't include this)
            try:
                unenc = db.unencrypted_vms(cluster_id=c["id"])
                unencrypted_samples.append(({"cluster": c["name"]}, len(unenc)))
            except Exception:
                unencrypted_samples.append(({"cluster": c["name"]}, 0))

            restore_overdue_samples.append(({"cluster": c["name"]}, restore_overdue))

        if severity_samples:
            output_parts.append(
                _fmt_metric(
                    "backup_sentinel_vm_backup_severity_count",
                    "Number of VMs by backup severity per cluster",
                    "gauge",
                    severity_samples,
                )
            )
        if unencrypted_samples:
            output_parts.append(
                _fmt_metric(
                    "backup_sentinel_unencrypted_backups_count",
                    "Number of VMs with unencrypted backups",
                    "gauge",
                    unencrypted_samples,
                )
            )
        if restore_overdue_samples:
            output_parts.append(
                _fmt_metric(
                    "backup_sentinel_restore_overdue_count",
                    "Number of VMs with overdue restore tests",
                    "gauge",
                    restore_overdue_samples,
                )
            )
    except Exception:
        pass

    body = "\n\n".join(p for p in output_parts if p) + "\n"
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4; charset=utf-8")
