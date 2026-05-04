from __future__ import annotations

import time as _time
from datetime import datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError as PydanticValidationError
from starlette import status

from app import db
from app.models import RestoreTestCreate
from app.services.bootstrap import slugify_cluster
from app.services.proxmox_progress import fetch_running_backup_progress
from app.services.reporting import build_compliance_summary
from app.web.common import common_context, settings_actor, templates
from app.web.sync import background_sync, sync_executor, sync_logs, syncing_clusters

router = APIRouter()

# Cache für sync-status: cluster_slug → (timestamp, result_dict)
_sync_status_cache: dict[str, tuple[float, dict]] = {}
_SYNC_STATUS_TTL = 2.0  # Sekunden


@router.post("/clusters/{cluster_slug}/rename")
def rename_cluster(request: Request, cluster_slug: str, name: str = Form(...)) -> RedirectResponse:
    cluster = db.get_cluster_by_slug(cluster_slug)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    new_slug = slugify_cluster(name)
    if new_slug != cluster_slug:
        existing = db.get_cluster_by_slug(new_slug)
        if existing:
            raise HTTPException(status_code=409, detail="Ein Cluster mit diesem Namen existiert bereits.")
    old_name = cluster["name"]
    new_name = name.strip()
    db.rename_cluster(cluster["id"], new_name, new_slug)
    try:
        db.append_audit_event(
            settings_actor(request),
            "cluster.renamed",
            cluster_slug,
            f"old={old_name}, new={new_name}",
        )
    except Exception:
        pass
    return RedirectResponse(url="/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/clusters/{cluster_slug}/delete")
def delete_cluster(request: Request, cluster_slug: str) -> RedirectResponse:
    cluster = db.get_cluster_by_slug(cluster_slug)
    cluster_name = cluster["name"] if cluster else cluster_slug
    deleted = db.delete_cluster(cluster_slug)
    if not deleted:
        raise HTTPException(status_code=404, detail="Cluster not found")
    try:
        db.append_audit_event(
            settings_actor(request),
            "cluster.deleted",
            cluster_slug,
            f"name={cluster_name}",
        )
    except Exception:
        pass
    return RedirectResponse(url="/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/clusters/{cluster_slug}/sync")
def sync_cluster(cluster_slug: str, next: str = Form(default="")) -> RedirectResponse:
    cluster = db.get_cluster_by_slug(cluster_slug)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    if not cluster["api_user"] or not cluster["token_id"] or not cluster["token_secret"]:
        raise HTTPException(status_code=409, detail="Cluster is not fully registered")
    if cluster["id"] not in syncing_clusters:
        syncing_clusters.add(cluster["id"])
        sync_executor.submit(background_sync, cluster)
    redirect_url = next if next in ("/settings",) else f"/clusters/{cluster_slug}"
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/api/clusters/{cluster_slug}/sync-status")
def get_sync_status(cluster_slug: str, since: int = 0):
    cluster = db.get_cluster_by_slug(cluster_slug)
    if not cluster:
        raise HTTPException(status_code=404)
    cid = cluster["id"]

    now = _time.monotonic()
    cache_entry = _sync_status_cache.get(cluster_slug)
    if cache_entry is None or (now - cache_entry[0]) >= _SYNC_STATUS_TTL:
        buf = list(sync_logs.get(cid, []))
        progress = fetch_running_backup_progress(cluster, [node["name"] for node in db.list_nodes(cluster["id"])])
        serializable_progress = {
            str(vmid): {
                "label": item.get("label", "Backup läuft"),
                "title": item.get("title", ""),
                "percent": item.get("percent"),
            }
            for vmid, item in progress.items()
        }
        cached = {
            "syncing": cid in syncing_clusters,
            "log": buf,
            "offset": len(buf),
            "backup_progress": serializable_progress,
        }
        _sync_status_cache[cluster_slug] = (now, cached)
    else:
        cached = cache_entry[1]

    return {**cached, "log": cached["log"][since:]}


@router.get("/clusters/{cluster_slug}", response_class=HTMLResponse)
def cluster_detail(request: Request, cluster_slug: str) -> HTMLResponse:
    cluster = db.get_cluster_by_slug(cluster_slug)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    vm_rows = db.vm_governance_rows(cluster["id"])
    live_backup_progress = fetch_running_backup_progress(
        cluster, [node["name"] for node in db.list_nodes(cluster["id"])]
    )
    for row in vm_rows:
        row["live_backup_progress"] = live_backup_progress.get(row["vmid"])
    restore_tests = db.recent_restore_tests(cluster["id"])
    encryption = db.encryption_audit(cluster["id"])
    context = {
        "cluster": cluster,
        "nodes": db.list_nodes(cluster["id"]),
        "vm_rows": vm_rows,
        "restore_tests": restore_tests,
        "compliance_summary": build_compliance_summary(vm_rows, restore_tests),
        "pbs_connections": db.list_pbs_connections(cluster["id"]),
        "sparklines": db.vm_backup_sparkline_data(cluster["id"], days=31),
        "encryption": encryption,
        "unencrypted_vms": db.unencrypted_vms(cluster["id"]) if encryption["pct_encrypted"] < 100 else [],
    }
    return templates.TemplateResponse(request, "cluster.html", common_context(request) | context)


@router.post("/clusters/{cluster_slug}/restore-tests")
def add_restore_test(
    request: Request,
    cluster_slug: str,
    vm_id: int = Form(...),
    tested_at: str = Form(...),
    result: str = Form(...),
    recovery_type: str = Form(...),
    duration_minutes: int = Form(...),
    evidence_note: str = Form(""),
) -> RedirectResponse:
    cluster = db.get_cluster_by_slug(cluster_slug)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    try:
        parsed_date = datetime.fromisoformat(tested_at).date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Ungültiges Datum") from exc
    try:
        payload = RestoreTestCreate(
            vm_id=vm_id,
            tested_at=parsed_date,
            result=result,
            recovery_type=recovery_type,
            duration_minutes=duration_minutes,
            evidence_note=evidence_note,
        )
    except PydanticValidationError as exc:
        # Surface the first validation problem to the user instead of a 500.
        first = exc.errors()[0]
        raise HTTPException(
            status_code=400,
            detail=f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}",
        ) from exc
    db.create_restore_test(**payload.model_dump())
    try:
        vm = db.get_vm_for_cluster(cluster["id"], vm_id)
        vmid_value = vm["vmid"] if vm else vm_id
        db.append_audit_event(
            settings_actor(request),
            "restore_test.added",
            f"{cluster_slug}/{vmid_value}",
            f"result={result}, recovery_type={recovery_type}, duration={duration_minutes}min",
        )
    except Exception:
        pass
    return RedirectResponse(url=f"/clusters/{cluster_slug}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/clusters/{cluster_slug}/backup-policy")
def update_backup_policy(
    cluster_slug: str,
    vm_id: int = Form(...),
    backup_policy_override: str = Form(...),
) -> RedirectResponse:
    cluster = db.get_cluster_by_slug(cluster_slug)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    vm = db.get_vm_for_cluster(cluster["id"], vm_id)
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    normalized = backup_policy_override.strip().lower()
    if normalized not in {"", "daily", "weekly", "none"}:
        raise HTTPException(status_code=400, detail="Unsupported backup policy")
    db.update_vm_backup_policy(vm_id, normalized or None)
    return RedirectResponse(url=f"/clusters/{cluster_slug}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/clusters/{cluster_slug}/pbs/{pbs_id}/delete")
def delete_pbs_connection(request: Request, cluster_slug: str, pbs_id: int) -> RedirectResponse:
    pbs_name = str(pbs_id)
    try:
        cluster = db.get_cluster_by_slug(cluster_slug)
        if cluster:
            for conn in db.list_pbs_connections(cluster["id"]):
                if conn["id"] == pbs_id:
                    pbs_name = conn["name"]
                    break
    except Exception:
        pass
    db.delete_pbs_connection(pbs_id)
    try:
        db.append_audit_event(
            settings_actor(request),
            "pbs.deleted",
            cluster_slug,
            f"pbs_name={pbs_name}",
        )
    except Exception:
        pass
    return RedirectResponse(url="/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/api/clusters/{cluster_slug}/bootstrap-status")
def bootstrap_status(cluster_slug: str) -> JSONResponse:
    cluster = db.get_cluster_by_slug(cluster_slug)
    if not cluster:
        raise HTTPException(status_code=404)
    pbs_conns = db.list_pbs_connections(cluster["id"])
    return JSONResponse(
        {
            "pve_registered": cluster["registered_at"] is not None,
            "pbs_count": len(pbs_conns),
            "last_pve_sync_ok": cluster.get("last_pve_sync_ok"),
        }
    )
