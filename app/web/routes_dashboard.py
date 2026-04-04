from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import db
from app.services.reporting import build_compliance_summary
from app.web.common import common_context, templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    clusters = db.cluster_summaries()
    vm_rows = db.vm_governance_rows()
    vm_rows.sort(key=lambda row: (
        row["backup_severity"], row["restore_due_status"], row["cluster_name"], row["vm_name"],
    ))
    restore_tests = db.recent_restore_tests()
    compliance_summary = build_compliance_summary(vm_rows, restore_tests)

    # Batch-fetch all PBS connections and cluster data for health bar
    all_clusters = db.list_clusters()
    all_pbs = db.list_pbs_connections()
    pbs_by_cluster: dict[int, list[dict]] = {}
    for p in all_pbs:
        pbs_by_cluster.setdefault(p["cluster_id"], []).append(p)

    health_bar: list[dict] = []
    for cluster in all_clusters:
        pbs_conns = pbs_by_cluster.get(cluster["id"], [])
        pbs_all_ok = all(p.get("last_sync_ok") for p in pbs_conns) if pbs_conns else True
        pve_ok = cluster.get("last_pve_sync_ok")
        if pve_ok and pbs_all_ok:
            health = "ok"
        elif pve_ok is None:
            health = "unknown"
        elif pve_ok is False or not pbs_all_ok:
            health = "critical"
        else:
            health = "ok"
        sync_health = db.get_cluster_sync_health(cluster["id"])
        health_bar.append({
            "name": cluster["name"],
            "slug": cluster["slug"],
            "health": health,
            "last_sync": cluster.get("last_pve_sync_at"),
            "pbs_count": len(pbs_conns),
            "pbs_ok": sum(1 for p in pbs_conns if p.get("last_sync_ok")),
            "stale": sync_health["stale"],
            "est_failures": sync_health["failures"],
        })

    context = {
        "clusters": clusters,
        "vm_rows": vm_rows,
        "generated_at": datetime.now(UTC),
        "restore_tests": restore_tests[:10],
        "compliance_summary": compliance_summary,
        "health_bar": health_bar,
        "encryption": db.encryption_audit(),
        "restore_coverage": db.restore_test_coverage(),
    }
    return templates.TemplateResponse("dashboard.html", common_context(request) | context)
