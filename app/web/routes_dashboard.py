from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import db
from app.config import SYNC_INTERVAL_MINUTES
from app.services import sync_progress
from app.services.backup_status import pbs_sync_state, sync_freshness
from app.services.proxmox_progress import cached_running_backups
from app.services.reporting import build_compliance_summary
from app.web.common import common_context, templates
from app.web.sync import refresh_sync_job_states_soon

router = APIRouter()


def _encryption_total(summaries: list[dict]) -> dict[str, int]:
    """All clusters together, from the per-cluster counts already loaded."""
    total = sum(s["encryption"]["total_backups"] for s in summaries)
    encrypted = sum(s["encryption"]["encrypted_backups"] for s in summaries)
    return {
        "total_backups": total,
        "encrypted_backups": encrypted,
        "pct_encrypted": round(encrypted / total * 100) if total else 100,
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    # The per-VM governance query is the expensive one: run it once and hand
    # it to everything on this page that needs it.
    all_clusters = db.list_clusters()
    vm_rows = db.vm_governance_rows()
    clusters = db.cluster_summaries(all_clusters, vm_rows)
    vm_rows.sort(
        key=lambda row: (
            row["backup_severity"],
            row["restore_due_status"],
            row["cluster_name"],
            row["vm_name"],
        )
    )
    restore_tests = db.recent_restore_tests()
    compliance_summary = build_compliance_summary(vm_rows, restore_tests)

    # Running backups live from PVE: the sync's in-progress markers expire
    # after 6 h, so long backups would vanish. Unreachable clusters keep them.
    registered = [c for c in all_clusters if c.get("token_secret")]
    nodes_by_cluster = db.node_names_by_cluster()
    live = cached_running_backups([(c, nodes_by_cluster.get(c["id"], [])) for c in registered])
    slug_by_id = {c["id"]: c["slug"] for c in all_clusters}
    names = {(row["cluster_id"], row["vmid"]): row["vm_name"] for row in vm_rows}
    running_by_slug = {
        slug_by_id[cid]: [names.get((cid, vmid), str(vmid)) for vmid in sorted(progress)]
        for cid, progress in live.items()
        if progress is not None
    }
    id_by_slug = {c["slug"]: c["id"] for c in all_clusters}
    pbs_by_cluster: dict[int, list[dict]] = {}
    for conn in db.list_pbs_connections():
        pbs_by_cluster.setdefault(conn["cluster_id"], []).append(conn)
    now = datetime.now(UTC)
    # Replications started since the last sync show from the next page load.
    refresh_sync_job_states_soon(list(id_by_slug.values()))
    for summary in clusters:
        if summary["cluster_slug"] in running_by_slug:
            summary["running_vms"] = running_by_slug[summary["cluster_slug"]]
        # Replication (PBS sync jobs) running into this cluster's datastores,
        # from the precomputed diagram: one stored row per cluster, no PBS call.
        summary["sync_freshness"] = sync_freshness(
            summary["last_pve_sync_success_at"], summary["last_pve_sync_ok"], now, SYNC_INTERVAL_MINUTES
        )
        cid = id_by_slug.get(summary["cluster_slug"])
        summary["pbs_syncs"] = [
            {
                "name": conn["name"],
                "state": pbs_sync_state(conn, now, SYNC_INTERVAL_MINUTES),
                "at": conn.get("last_sync_at"),
                "ok": conn.get("last_sync_ok"),
            }
            for conn in pbs_by_cluster.get(cid, [])
        ]
        summary["running_syncs"] = sync_progress.running_syncs(cid) if cid is not None else []

    context = {
        "clusters": clusters,
        "vm_rows": vm_rows,
        "generated_at": datetime.now(UTC),
        "restore_tests": restore_tests[:10],
        "compliance_summary": compliance_summary,
        "encryption": _encryption_total(clusters),
        "restore_coverage": db.restore_test_coverage(vm_rows),
    }
    return templates.TemplateResponse(request, "dashboard.html", common_context(request) | context)
