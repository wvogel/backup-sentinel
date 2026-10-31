"""Routes for automated restore testing: restore-server config, per-VM controls,
the runs view, and held-environment actions."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette import status

from app import db, db_restore
from app.services import restore_config, restore_orchestrator
from app.web.common import common_context, templates

logger = logging.getLogger(__name__)
router = APIRouter()


def _target_storages_from_rows(names: list[str], classes: list[str], used: set[str]) -> list[dict]:
    """Build the target-storage list from the Settings table: the app enumerated
    the storages, the operator only ticked which to use and assigned a disk
    class. Order follows table (selection) order."""
    out = []
    order = 0
    for name, cls in zip(names, classes, strict=False):
        if name in used:
            out.append({"storage": name, "disk_class": cls or "standard-ssd", "order": order})
            order += 1
    return out


@router.post("/settings/restore-server")
def save_restore_server(
    request: Request,
    ssh_host: str = Form(...),
    ssh_port: int = Form(default=22),
    ssh_user: str = Form(default="root"),
    ssh_private_key: str = Form(default=""),
    ssh_host_key: str = Form(default=""),
    ts_name: list[str] = Form(default=[]),
    ts_class: list[str] = Form(default=[]),
    ts_use: list[str] = Form(default=[]),
    sandbox_bridge: str = Form(default="vmbr9"),
    sandbox_vlan_from: int = Form(default=900),
    sandbox_vlan_to: int = Form(default=999),
    restore_vmid_from: int = Form(default=9000),
    restore_vmid_to: int = Form(default=9100),
    mgmt_iface: str = Form(...),
    mgmt_vlan: str = Form(default=""),
    mgmt_ip_from: str = Form(...),
    mgmt_ip_to: str = Form(...),
    reprovision: str = Form(default=""),
) -> RedirectResponse:
    # No API fields: everything the API needs is obtained over SSH. We derive the
    # API URL from the SSH host and provision the token via SSH.
    ssh_host = ssh_host.strip()
    existing = restore_config.load() or {}
    payload = {
        "name": ssh_host or "PVE-Restore",
        "api_url": f"https://{ssh_host}:8006",
        "api_user": existing.get("api_user", "root@pam"),
        "token_id": existing.get("token_id", ""),
        "token_secret": "",  # kept from existing unless (re)provisioned below
        "ssh_host": ssh_host,
        "ssh_port": ssh_port,
        "ssh_user": ssh_user,
        "ssh_private_key": ssh_private_key,
        "ssh_host_key": ssh_host_key,
        "target_storages": _target_storages_from_rows(ts_name, ts_class, set(ts_use)),
        "sandbox_bridge": sandbox_bridge,
        "sandbox_vlan_from": sandbox_vlan_from,
        "sandbox_vlan_to": sandbox_vlan_to,
        "restore_vmid_from": restore_vmid_from,
        "restore_vmid_to": restore_vmid_to,
        "mgmt_iface": mgmt_iface,
        "mgmt_vlan": int(mgmt_vlan) if mgmt_vlan.strip() else None,
        # The sandbox never talks outside: no gateway, always /24.
        "mgmt_gateway": "",
        "mgmt_netmask": "255.255.255.0",
        "mgmt_ip_from": mgmt_ip_from,
        "mgmt_ip_to": mgmt_ip_to,
    }
    try:
        # Provision the API token over SSH on first setup, or when asked to renew.
        if reprovision == "1" or not existing.get("token_secret"):
            key = ssh_private_key or existing.get("ssh_private_key", "")
            if not ssh_host or not key:
                raise ValueError("SSH host and key are required")
            sshcfg = restore_orchestrator.ensure_host_key_pinned(
                {
                    "ssh_host": ssh_host,
                    "ssh_port": ssh_port,
                    "ssh_user": ssh_user,
                    "ssh_private_key": key,
                    "ssh_host_key": ssh_host_key or existing.get("ssh_host_key", ""),
                }
            )
            payload["ssh_host_key"] = sshcfg.get("ssh_host_key", payload["ssh_host_key"])
            tok = restore_orchestrator.build_server(sshcfg).create_api_token()
            payload["api_user"] = tok["api_user"]
            payload["token_id"] = tok["token_id"]
            payload["token_secret"] = tok["token_secret"]
        restore_config.save(payload)
        db.save_settings({"restore_server.last_error": ""})
        query = "restore=saved"
        # Run the connection test right away so the "saved" modal can show the
        # live state (a fresh token especially) instead of a stale result. A
        # hiccup here must not turn a successful save into an error.
        try:
            _run_and_store_test()
        except Exception as exc:
            logger.debug("post-save connection test failed: %s", exc)
    except Exception as exc:
        logger.warning("restore-server config save/provision failed: %s", exc)
        detail = f"{type(exc).__name__}: {exc}"
        db.save_settings({"restore_server.last_error": detail[:500]})
        query = "restore_error=1"
    return RedirectResponse(f"/settings?{query}#restore", status_code=status.HTTP_303_SEE_OTHER)


def _run_and_store_test() -> dict:
    """Run the restore-server connection test and persist it as last_test
    (human-readable local timestamp). Returns the stored result."""
    import json
    from datetime import UTC, datetime

    from app.web.common import datetime_local

    result = restore_orchestrator.test_connection()
    result["at"] = datetime_local(datetime.now(UTC))  # human, local tz (e.g. 31.10.2026 01:31)
    db.save_settings({"restore_server.last_test": json.dumps(result)})
    return result


@router.post("/settings/restore-server/test")
def test_restore_server(request: Request) -> RedirectResponse:
    _run_and_store_test()
    return RedirectResponse("/settings#restore", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/settings/restore-server/clear-host-key")
def clear_host_key(request: Request) -> RedirectResponse:
    restore_config.set_host_key("")
    return RedirectResponse("/settings#restore", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/restore", response_class=HTMLResponse)
def restore_runs(request: Request) -> HTMLResponse:
    runs = db_restore.list_runs(limit=50)
    held = db_restore.list_runs(limit=50, status="held")
    host = None
    config = restore_config.load()
    if config is not None:
        try:
            host = restore_orchestrator.build_server(config).free_resources()
        except Exception as exc:
            logger.debug("free_resources failed: %s", exc)
    context = {
        "runs": runs,
        "held": held,
        "host": host,
        "configured": config is not None,
        "queue_paused": restore_config.queue_paused(),
    }
    return templates.TemplateResponse(request, "restore.html", common_context(request) | context)


@router.post("/restore/vm/{vm_id}/test")
def manual_test(request: Request, vm_id: int) -> RedirectResponse:
    db_restore.create_run(vm_id, trigger="manual")
    try:
        from app.web.sync import _kick_restore_queue

        _kick_restore_queue()
    except Exception as exc:
        logger.debug("kick restore queue failed: %s", exc)
    return RedirectResponse("/restore", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/restore/queue/pause")
def pause_queue(request: Request, paused: str = Form(default="1")) -> RedirectResponse:
    restore_config.set_queue_paused(paused == "1")
    return RedirectResponse("/restore", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/restore/runs/{run_id}/reap")
def reap_run(request: Request, run_id: int) -> RedirectResponse:
    _held_action(run_id, "reap")
    return RedirectResponse("/restore", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/restore/runs/{run_id}/promote")
def promote_run(request: Request, run_id: int) -> RedirectResponse:
    _held_action(run_id, "promote")
    return RedirectResponse("/restore", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/restore/runs/{run_id}/retest")
def retest_run(request: Request, run_id: int) -> RedirectResponse:
    _held_action(run_id, "retest")
    return RedirectResponse("/restore", status_code=status.HTTP_303_SEE_OTHER)


def _held_action(run_id: int, action: str) -> None:
    from app.services import restore_worker

    run = db_restore.get_run(run_id)
    config = restore_config.load()
    if run is None or config is None or run["status"] != "held":
        return
    try:
        server = restore_orchestrator.build_server(config)
        plan = restore_orchestrator.plan_from_run(run, config)
        if action == "reap":
            restore_worker.reap_held(run_id, plan, server)
        elif action == "promote":
            restore_worker.promote_held(run_id, plan, server)
        elif action == "retest":
            result = restore_worker.retest_held(run_id, server, plan)
            if result == "passed":
                restore_worker.promote_held(run_id, plan, server)
    except Exception as exc:
        logger.warning("held action %s on run %s failed: %s", action, run_id, exc)


@router.post("/restore/vm/{vm_id}/policy")
def update_vm_restore_policy(
    request: Request,
    vm_id: int,
    auto_restore_enabled: str = Form(default=""),
    restore_cadence: str = Form(default="half_year"),
    probe_primary_ip: str = Form(default=""),
    probe_tcp_ports: str = Form(default=""),
    probe_require_icmp: str = Form(default=""),
    hold_on_failure: str = Form(default=""),
    restore_retry_days: int = Form(default=7),
    redirect: str = Form(default="/restore"),
) -> RedirectResponse:
    ports = [int(p) for p in probe_tcp_ports.replace(",", " ").split() if p.strip().isdigit()]
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO vm_policies
                (vm_id, owner_name, criticality, rpo_hours, auto_restore_enabled, restore_cadence,
                 probe_primary_ip, probe_tcp_ports, probe_require_icmp, hold_on_failure, restore_retry_days)
            VALUES (%s, 'unassigned', 'medium', 24, %s, %s, NULLIF(%s, ''), %s, %s, %s, %s)
            ON CONFLICT (vm_id) DO UPDATE SET
                auto_restore_enabled = EXCLUDED.auto_restore_enabled,
                restore_cadence = EXCLUDED.restore_cadence,
                probe_primary_ip = EXCLUDED.probe_primary_ip,
                probe_tcp_ports = EXCLUDED.probe_tcp_ports,
                probe_require_icmp = EXCLUDED.probe_require_icmp,
                hold_on_failure = EXCLUDED.hold_on_failure,
                restore_retry_days = EXCLUDED.restore_retry_days
            """,
            (
                vm_id,
                bool(auto_restore_enabled),
                restore_cadence,
                probe_primary_ip,
                ports,
                bool(probe_require_icmp),
                bool(hold_on_failure),
                restore_retry_days,
            ),
        )
        conn.commit()
    return RedirectResponse(redirect, status_code=status.HTTP_303_SEE_OTHER)
