from __future__ import annotations

import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from app import db
from app.i18n import ts
from app.models import BootstrapFinalize, BootstrapPayload, ClusterCreate, PBSFinalize
from app.services.bootstrap import build_enrollment_bundle, slugify_cluster
from app.web.common import settings_actor
from app.web.sync import background_sync, sync_executor, syncing_clusters

router = APIRouter()

_ENROLLMENT_REFUSED = (
    "Enrollment secret wrong or expired. Create a new onboarding command in the settings (it is valid for 24 hours)."
)


@router.post("/api/clusters", response_model=BootstrapPayload)
def create_cluster(request: Request, payload: ClusterCreate) -> BootstrapPayload:
    cluster_slug = slugify_cluster(payload.name)
    existing = db.get_cluster_by_slug(cluster_slug)
    if existing:
        raise HTTPException(status_code=409, detail="Cluster slug already exists")
    bundle = build_enrollment_bundle(cluster_slug)
    db.create_cluster(payload.name, cluster_slug, payload.api_url, bundle.enrollment_secret)
    try:
        db.append_audit_event(
            settings_actor(request),
            "cluster.created",
            cluster_slug,
            f"name={payload.name}, api_url={payload.api_url}",
        )
    except Exception:
        pass
    return BootstrapPayload(
        cluster_slug=cluster_slug,
        server_url=bundle.server_url,
        enrollment_secret=bundle.enrollment_secret,
        script_url=bundle.script_url,
        commands=bundle.commands,
    )


@router.post("/api/bootstrap/finalize")
def finalize_bootstrap(payload: BootstrapFinalize) -> JSONResponse:
    cluster = db.get_cluster_by_slug(payload.cluster_slug)
    if not cluster:
        raise HTTPException(status_code=404, detail="Unknown cluster")
    if not db.enrollment_secret_matches(cluster, payload.enrollment_secret):
        raise HTTPException(status_code=403, detail=_ENROLLMENT_REFUSED)
    if cluster["registered_at"] is not None:
        raise HTTPException(
            status_code=409,
            detail=ts("errors.cluster_already_registered"),
        )
    db.finalize_cluster_registration(payload.model_dump())
    cluster = db.get_cluster_by_slug(payload.cluster_slug)
    sync_status = {"status": "skipped", "nodes": 0, "vms": 0}
    if cluster and cluster["api_user"] and cluster["token_id"] and cluster["token_secret"]:
        if cluster["id"] not in syncing_clusters:
            syncing_clusters.add(cluster["id"])
            sync_executor.submit(background_sync, cluster)
            sync_status = {"status": "started", "nodes": 0, "vms": 0}
        else:
            sync_status = {"status": "already_running", "nodes": 0, "vms": 0}
    try:
        node_count = 0
        vm_count = 0
        if cluster:
            try:
                node_count = len(db.list_nodes(cluster["id"]))
            except Exception:
                pass
            try:
                vm_count = len(db.vm_governance_rows(cluster["id"]))
            except Exception:
                pass
        db.append_audit_event(
            "bootstrap-agent",
            "cluster.bootstrapped",
            payload.cluster_slug,
            f"node_count={node_count}, vm_count={vm_count}",
        )
    except Exception:
        pass
    return JSONResponse(
        {
            "status": "ok",
            "registered_at": datetime.now(UTC).isoformat(),
            "inventory_sync": sync_status,
        }
    )


@router.post("/api/bootstrap/pbs-finalize")
def pbs_bootstrap_finalize(body: PBSFinalize) -> JSONResponse:
    """Register a PBS. POST with a JSON body: the secrets must not travel in the
    URL, where proxies log them."""
    cluster_slug = body.cluster_slug
    cluster = db.get_cluster_by_slug(cluster_slug)
    if not cluster:
        raise HTTPException(status_code=404, detail="Unknown cluster slug")
    if not db.enrollment_secret_matches(cluster, body.enrollment_secret):
        raise HTTPException(status_code=403, detail=_ENROLLMENT_REFUSED)
    if not body.api_url.lower().startswith("https://"):
        raise HTTPException(status_code=400, detail="api_url must be an https:// URL")
    payload = {
        "name": body.name,
        "api_url": body.api_url,
        "api_token_id": body.api_token_id,
        "api_token_secret": body.api_token_secret,
        "fingerprint": body.fingerprint,
    }
    if body.namespace is not None:
        payload["namespace"] = db.normalize_namespace(body.namespace)
    db.upsert_pbs_connection(cluster["id"], payload)
    try:
        db.append_audit_event(
            "bootstrap-agent",
            "pbs.added",
            cluster_slug,
            f"pbs_name={payload['name']}, api_url={payload['api_url']}",
        )
    except Exception:
        pass
    try:
        db.append_audit_event(
            "bootstrap-agent",
            "pbs.bootstrapped",
            cluster_slug,
            f"pbs_name={payload['name']}",
        )
    except Exception:
        pass
    if cluster["api_user"] and cluster["token_id"] and cluster["token_secret"]:
        if cluster["id"] not in syncing_clusters:
            syncing_clusters.add(cluster["id"])
            sync_executor.submit(background_sync, cluster)
    return JSONResponse({"status": "ok", "cluster": cluster["name"]})


@router.post("/clusters/{cluster_slug}/enrollment/renew")
def renew_enrollment(request: Request, cluster_slug: str) -> RedirectResponse:
    """A fresh enrollment secret for adding a PBS (admin only, app.web.auth)."""
    cluster = db.get_cluster_by_slug(cluster_slug)
    if not cluster:
        raise HTTPException(status_code=404, detail="Unknown cluster")
    db.renew_enrollment(cluster["id"], secrets.token_urlsafe(24))
    try:
        db.append_audit_event(settings_actor(request), "cluster.enrollment_renewed", cluster_slug)
    except Exception:
        pass
    return RedirectResponse(url=f"/settings?enroll={cluster['id']}", status_code=303)


@router.get("/bootstrap/proxmox-agent.sh", response_class=PlainTextResponse)
def proxmox_bootstrap_script() -> PlainTextResponse:
    return PlainTextResponse(db.bootstrap_script(), media_type="text/x-shellscript")


@router.get("/bootstrap/pbs-agent.sh", response_class=PlainTextResponse)
def pbs_bootstrap_script() -> PlainTextResponse:
    return PlainTextResponse(db.pbs_bootstrap_script(), media_type="text/x-shellscript")
