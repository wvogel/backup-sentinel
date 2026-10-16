from __future__ import annotations

import time as _time
from datetime import datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError as PydanticValidationError
from starlette import status

from app import db
from app.i18n import t
from app.models import RestoreTestCreate
from app.services import topology_store
from app.services.backup_status import RESTORE_CLASSES, assess_slow_backup, format_duration
from app.services.bootstrap import slugify_cluster
from app.services.pbs import sync_task_state
from app.services.proxmox_progress import running_backup_progress_cached, running_backup_progress_peek
from app.services.reporting import build_compliance_summary
from app.web.common import common_context, datetime_local, settings_actor, templates
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


def running_backup_info(progress: dict | None, typical_seconds: int | None = None) -> dict:
    """Labels for a running backup: banner tile lines, slow warning and row tooltip."""
    progress = progress or {}
    slow = assess_slow_backup(progress, typical_seconds)
    percent = progress.get("percent")
    details: list[str] = []
    if progress.get("written_label") and progress.get("target_label"):
        details.append(t("progress.written_of", written=progress["written_label"], total=progress["target_label"]))
    if progress.get("eta_short"):
        details.append(t("cluster.running_until", time=progress["eta_short"]))
    rates: list[str] = []
    if progress.get("read_label") and progress.get("write_label"):
        if progress["read_label"] == progress["write_label"]:
            rates.append(t("cluster.running_rw", rate=progress["read_label"]))
        else:
            rates.append(t("cluster.running_read", rate=progress["read_label"]))
            rates.append(t("cluster.running_write", rate=progress["write_label"]))
        details.append(" · ".join(rates))
    parts = ([t("cluster.running_percent", pct=percent)] if percent is not None else []) + details
    return {
        "percent": percent,
        # Tile lines in the running banner (size, end, throughput).
        "details": details or [t("cluster.backup_running")],
        "line": " · ".join(parts) or t("cluster.backup_running"),
        "warning": t("cluster.running_slow", typical=format_duration(slow[0])) if slow else "",
        "title": progress.get("title", ""),
    }


def running_count_label(count: int) -> str:
    return t("cluster.running_one" if count == 1 else "cluster.running_other", count=count)


_sync_progress_cache: dict[str, tuple[float, tuple[float | None, bool]]] = {}
_SYNC_PROGRESS_TTL = 30.0


@router.get("/api/clusters/{cluster_slug}/sync-progress")
def get_sync_progress(cluster_slug: str) -> JSONResponse:
    """Live progress of the replication syncs running in this cluster's
    diagram, keyed "pbs_id:job_id" (cached 30 s per task)."""
    cluster = db.get_cluster_by_slug(cluster_slug)
    if not cluster:
        raise HTTPException(status_code=404)
    conns = {c["id"]: c for c in db.list_pbs_connections()}
    result: dict[str, dict] = {}
    for edge in topology_store.load(cluster["id"])["edges"]:
        upid, conn = edge.get("running_upid"), conns.get(edge.get("pbs_id"))
        if edge.get("state") != "running" or not upid or not conn:
            continue
        hit = _sync_progress_cache.get(upid)
        if hit is None or _time.monotonic() - hit[0] >= _SYNC_PROGRESS_TTL:
            try:
                hit = (_time.monotonic(), sync_task_state(dict(conn), upid))
            except Exception:  # PBS unreachable: keep what the page shows
                continue
            _sync_progress_cache[upid] = hit
        percent, finished = hit[1]
        result[f"{edge['pbs_id']}:{edge['job_id']}"] = {"percent": percent, "finished": finished}
    return JSONResponse(result)


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
        progress, _ = running_backup_progress_cached(cluster, [node["name"] for node in db.list_nodes(cluster["id"])])
        typical = db.typical_backup_durations(cid) if progress else {}
        serializable_progress = {
            str(vmid): running_backup_info(item, typical.get(vmid)) for vmid, item in progress.items()
        }
        cached = {
            "syncing": cid in syncing_clusters,
            "log": buf,
            "offset": len(buf),
            "backup_progress": serializable_progress,
            "running_count_label": running_count_label(len(serializable_progress)),
        }
        _sync_status_cache[cluster_slug] = (now, cached)
    else:
        cached = cache_entry[1]

    return {**cached, "log": cached["log"][since:]}


@router.get("/api/clusters/{cluster_slug}/vms/{vm_id}/history")
def vm_history(cluster_slug: str, vm_id: int) -> dict:
    cluster = db.get_cluster_by_slug(cluster_slug)
    if not cluster or not db.get_vm_for_cluster(cluster["id"], vm_id):
        raise HTTPException(status_code=404, detail="VM not found")
    events = db.vm_backup_history(vm_id, days=30)
    return {
        "events": [
            {
                "date": event["started_at"].isoformat(),
                "label": datetime_local(event["started_at"], "%d.%m. %H:%M"),
                "status": event["status"],
                "backups": event["backups"],
                "size_bytes": event["size_bytes"],
                "duration_seconds": event["duration_seconds"],
                "throughput_bps": event["throughput_bps"],
                "reused_pct": event["reused_pct"],
                "full_backup": event["full_backup"],
            }
            for event in events
        ]
    }


@router.get("/clusters/{cluster_slug}", response_class=HTMLResponse)
def cluster_detail(request: Request, cluster_slug: str) -> HTMLResponse:
    cluster = db.get_cluster_by_slug(cluster_slug)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    vm_rows = db.vm_governance_rows(cluster["id"])
    # Never wait for PVE here: the page's poll fills in progress right away
    # when nothing recent is cached.
    live_backup_progress, _ = running_backup_progress_peek(
        cluster, [node["name"] for node in db.list_nodes(cluster["id"])]
    )
    progress_pending = live_backup_progress is None
    live_backup_progress = live_backup_progress or {}
    running_backups = []
    typical = db.typical_backup_durations(cluster["id"]) if live_backup_progress else {}
    for row in vm_rows:
        row["live_backup_progress"] = live_backup_progress.get(row["vmid"])
        row["running"] = None
        if row["live_backup_progress"] or row["in_progress_since"]:
            row["running"] = running_backup_info(row["live_backup_progress"], typical.get(row["vmid"]))
            running_backups.append(row)
    restore_tests = db.recent_restore_tests(cluster["id"])
    encryption = db.encryption_audit(cluster["id"])
    context = {
        "cluster": cluster,
        "nodes": db.list_nodes(cluster["id"]),
        "vm_rows": vm_rows,
        "running_backups": running_backups,
        "running_count_label": running_count_label(len(running_backups)),
        "progress_pending": progress_pending,
        "restore_tests": restore_tests,
        "compliance_summary": build_compliance_summary(vm_rows, restore_tests),
        "pbs_connections": db.list_pbs_connections(cluster["id"]),
        "topology": topology_store.load(cluster["id"]),
        "storages_by_vmid": db.recent_storages_by_vmid(cluster["id"]),
        "sparklines": db.vm_backup_sparkline_data(cluster["id"], days=31),
        "encryption": encryption,
        "unencrypted_vms": db.unencrypted_vms(cluster["id"]) if encryption["pct_encrypted"] < 100 else [],
    }
    return templates.TemplateResponse(request, "cluster.html", common_context(request) | context)


@router.post("/clusters/{cluster_slug}/restore-tests")
def add_restore_test(
    request: Request,
    cluster_slug: str,
    vm_id: list[int] = Form(...),
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
    # Several VMs can be documented at once (e.g. a full restore drill).
    # Validate everything first so a bad entry doesn't leave half the tests saved.
    vms = [db.get_vm_for_cluster(cluster["id"], single_vm_id) for single_vm_id in vm_id]
    if not vms or any(vm is None for vm in vms):
        raise HTTPException(status_code=404, detail="VM not found")
    try:
        payloads = [
            RestoreTestCreate(
                vm_id=vm["id"],
                tested_at=parsed_date,
                result=result,
                recovery_type=recovery_type,
                duration_minutes=duration_minutes,
                evidence_note=evidence_note,
            )
            for vm in vms
        ]
    except PydanticValidationError as exc:
        # Surface the first validation problem to the user instead of a 500.
        first = exc.errors()[0]
        raise HTTPException(
            status_code=400,
            detail=f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}",
        ) from exc
    for vm, payload in zip(vms, payloads, strict=True):
        db.create_restore_test(**payload.model_dump())
        try:
            db.append_audit_event(
                settings_actor(request),
                "restore_test.added",
                f"{cluster_slug}/{vm['vmid']}",
                f"result={result}, recovery_type={recovery_type}, duration={duration_minutes}min",
            )
        except Exception:
            pass
    return RedirectResponse(url=f"/clusters/{cluster_slug}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/clusters/{cluster_slug}/restore-class")
def update_restore_class(
    cluster_slug: str,
    vm_id: int = Form(...),
    restore_class: str = Form(...),
) -> RedirectResponse:
    cluster = db.get_cluster_by_slug(cluster_slug)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    if not db.get_vm_for_cluster(cluster["id"], vm_id):
        raise HTTPException(status_code=404, detail="VM not found")
    if restore_class not in RESTORE_CLASSES:
        raise HTTPException(status_code=400, detail="Unsupported restore class")
    db.update_vm_restore_class(vm_id, restore_class)
    return RedirectResponse(url=f"/clusters/{cluster_slug}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/clusters/{cluster_slug}/backup-policy")
def update_backup_policy(
    cluster_slug: str,
    vm_id: int = Form(...),
    backup_policy_override: str = Form(""),
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


@router.post("/clusters/{cluster_slug}/pbs/{pbs_id}/settings")
def update_pbs_settings(
    request: Request, cluster_slug: str, pbs_id: int, namespace: str = Form(""), aliases: str = Form("")
) -> RedirectResponse:
    cluster = db.get_cluster_by_slug(cluster_slug)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    if not db.update_pbs_settings(cluster["id"], pbs_id, namespace, aliases):
        raise HTTPException(status_code=404, detail="PBS not found")
    topology_store.refresh_all()
    try:
        db.append_audit_event(
            settings_actor(request),
            "pbs.settings",
            cluster_slug,
            f"pbs_id={pbs_id}, namespace={db.normalize_namespace(namespace)},"
            f" aliases={','.join(db.normalize_aliases(aliases))}",
        )
    except Exception:
        pass
    return RedirectResponse(url="/settings", status_code=status.HTTP_303_SEE_OTHER)


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
    topology_store.refresh_all()
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
