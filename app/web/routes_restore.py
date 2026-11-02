"""Routes for automated restore testing: restore-server config, per-VM controls,
the runs view, and held-environment actions."""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette import status

from app import db, db_restore
from app.services import restore_config, restore_orchestrator
from app.web.common import common_context, templates

logger = logging.getLogger(__name__)
router = APIRouter()


def _target_storages_from_rows(names: list[str], classes: list[str], used: set[str]) -> list[dict]:
    """Build the storage list from the Settings table. Every (image-capable) row
    is kept with its disk class so the class — and its icon — is known everywhere,
    not just for ticked targets; ``use`` marks the restore targets and ``order``
    their fill order (ticked rows first, in table order)."""
    out = []
    order = 0
    for name, cls in zip(names, classes, strict=False):
        is_used = name in used
        out.append(
            {"storage": name, "disk_class": cls or "standard-ssd", "use": is_used, "order": order if is_used else 9999}
        )
        if is_used:
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
    mgmt_gateway: str = Form(default=""),
    mgmt_netmask: str = Form(default="255.255.255.0"),
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
        # The management leg talks outward (the app reaches the helper across
        # subnets), so it needs a real netmask AND gateway. (The sandbox leg is
        # the one that stays isolated — no gateway, always /24.)
        "mgmt_gateway": mgmt_gateway.strip(),
        "mgmt_netmask": mgmt_netmask.strip() or "255.255.255.0",
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


@router.post("/settings/restore-server/target-storages")
def save_target_storages(
    request: Request,
    ts_name: list[str] = Form(default=[]),
    ts_class: list[str] = Form(default=[]),
    ts_use: list[str] = Form(default=[]),
) -> JSONResponse:
    """Persist only the target-storage selection/classes/order (from the Settings
    table), so a checkbox, dropdown or drag change saves on the spot without a
    full-form submit or a page reload. Everything else in the config is kept."""
    existing = restore_config.load()
    if existing is None:
        return JSONResponse({"ok": False, "error": "not configured"}, status_code=409)
    existing["target_storages"] = _target_storages_from_rows(ts_name, ts_class, set(ts_use))
    try:
        restore_config.save(existing)
    except Exception as exc:
        logger.warning("target-storage save failed: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=400)
    return JSONResponse({"ok": True})


@router.post("/settings/restore-server/clear-host-key")
def clear_host_key(request: Request) -> RedirectResponse:
    restore_config.set_host_key("")
    return RedirectResponse("/settings#restore", status_code=status.HTTP_303_SEE_OTHER)


def _cache_host_snapshot(host: dict) -> None:
    """Persist the latest host capacity so the dashboard tile can show it
    without a live (slow, blockable) call to the restore host on every load."""
    import json
    from datetime import UTC, datetime

    try:
        db.save_settings(
            {"restore_server.host_snapshot": json.dumps({"at": datetime.now(UTC).isoformat(), "host": host})}
        )
    except Exception as exc:
        logger.debug("host snapshot cache failed: %s", exc)


def _cache_pbs_locations(entries: list) -> None:
    """Cache the restore host's canonical PBS backup locations so the cluster/VM
    list can show per-VM restorability (red cross) without a live host call."""
    import json

    try:
        db.save_settings({"restore_server.pbs_locations": json.dumps(sorted({e["location"] for e in entries}))})
    except Exception as exc:
        logger.debug("pbs locations cache failed: %s", exc)


# Live restore-host data (RAM ring, PBS entries, VM-match diagnostics) is fetched
# over root SSH now, where each `pvesh` call costs an SSH round-trip + Perl
# startup. Doing several of them synchronously on every /restore load made the
# page slow. So we keep the computed view in-process and refresh it in a
# background thread: the page always renders instantly from the last snapshot,
# and a stale snapshot triggers a refresh that the next load picks up.
_HOST_VIEW_TTL = 25.0  # seconds a snapshot is considered fresh
_host_view: dict = {"at": 0.0, "data": None, "error": None}
_host_view_lock = threading.Lock()
_host_view_refreshing = threading.Event()


def _compute_host_view(config: dict) -> dict:
    server = restore_orchestrator.build_server(config)
    host = server.free_resources()
    host_pbs = restore_orchestrator.restore_host_pbs_entries(server)
    matched = restore_orchestrator.matched_vms_by_location([e["location"] for e in host_pbs])
    for e in host_pbs:
        e["vms"] = matched.get(e["location"], [])
    host_locs = {e["location"] for e in host_pbs}
    vm_locations = restore_orchestrator.all_vm_backup_locations()
    for v in vm_locations:
        v["matched"] = v["location"] in host_locs
    return {"host": host, "host_pbs": host_pbs, "vm_locations": vm_locations}


def _refresh_host_view(config: dict) -> None:
    """Recompute the host view over SSH and cache it. Runs in a daemon thread so
    it never blocks a page render; only one refresh runs at a time."""
    if _host_view_refreshing.is_set():
        return
    _host_view_refreshing.set()
    try:
        data = _compute_host_view(config)
        with _host_view_lock:
            _host_view.update(at=time.monotonic(), data=data, error=None)
        _cache_host_snapshot(data["host"])
        _cache_pbs_locations(data["host_pbs"])
    except Exception as exc:
        logger.warning("restore host view refresh failed: %s", exc)
        with _host_view_lock:
            _host_view.update(at=time.monotonic(), error=f"{type(exc).__name__}: {exc}"[:300])
    finally:
        _host_view_refreshing.clear()


_RUNS_PER_PAGE_OPTIONS = (25, 50, 100, 200)
_RUNS_PER_PAGE_DEFAULT = 25
_PER_PAGE_COOKIE = "restore_per_page"


def _clamp_per_page(raw: str | None, fallback: int) -> int:
    """A valid page size from ``raw`` (query or cookie), else ``fallback``."""
    try:
        v = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return v if v in _RUNS_PER_PAGE_OPTIONS else fallback


def _page_window(page: int, pages: int, radius: int = 2) -> list[int | None]:
    """The page numbers to show in the pager: first, last and a window of
    ``radius`` around the current page, sorted, with ``None`` marking a gap (…)."""
    if pages <= 1:
        return [1]
    want = {1, pages, page} | {page + d for d in range(-radius, radius + 1)}
    nums = sorted(p for p in want if 1 <= p <= pages)
    out: list[int | None] = []
    prev = 0
    for p in nums:
        if prev and p - prev > 1:
            out.append(None)  # gap → ellipsis
        out.append(p)
        prev = p
    return out


@router.get("/restore", response_class=HTMLResponse)
def restore_runs(request: Request) -> HTMLResponse:
    # Server-side filtering + pagination: the runs log can grow large, so never
    # load it all — WHERE + LIMIT/OFFSET + COUNT.
    qp = request.query_params
    f_status = qp.get("f_status") or None
    f_trigger = qp.get("f_trigger") or None
    f_cluster = qp.get("f_cluster") or None
    f_q = (qp.get("f_q") or "").strip() or None
    try:
        page = max(1, int(qp.get("page", "1")))
    except ValueError:
        page = 1
    # Page size: an explicit ?per= wins (and is remembered in a cookie); otherwise
    # the last remembered choice, else the default.
    per_explicit = qp.get("per")
    per = _clamp_per_page(
        per_explicit if per_explicit is not None else request.cookies.get(_PER_PAGE_COOKIE), _RUNS_PER_PAGE_DEFAULT
    )
    total = db_restore.count_runs(status=f_status, trigger=f_trigger, cluster=f_cluster, vm_query=f_q)
    pages = max(1, -(-total // per))  # ceil
    page = min(page, pages)
    runs = db_restore.list_runs(
        limit=per,
        offset=(page - 1) * per,
        status=f_status,
        trigger=f_trigger,
        cluster=f_cluster,
        vm_query=f_q,
    )
    # Running + held are shown in their own sections and must NOT depend on the
    # table's page/filter, so fetch them separately.
    running = db_restore.list_runs(limit=20, status="running")
    held = db_restore.list_runs(limit=50, status="held")
    host = None
    host_error = None
    host_loading = False
    host_pbs: list = []
    vm_locations: list = []
    config = restore_config.load()
    if config is not None:
        # Render from the in-process snapshot (instant); refresh over SSH in the
        # background when it is stale, so the page never blocks on pvesh calls.
        with _host_view_lock:
            snap = dict(_host_view)
        if snap["data"] is None or (time.monotonic() - snap["at"]) > _HOST_VIEW_TTL:
            threading.Thread(target=_refresh_host_view, args=(config,), daemon=True).start()
        if snap["data"]:
            host = snap["data"]["host"]
            host_pbs = snap["data"]["host_pbs"]
            vm_locations = snap["data"]["vm_locations"]
        host_error = snap["error"] if snap["data"] is None else None
        # No snapshot yet (fresh app start) and no failure: the first refresh is
        # still running — show "loading", not "unreachable".
        host_loading = snap["data"] is None and snap["error"] is None
    context = {
        "runs": runs,
        "running": running,
        "held": held,
        "host": host,
        "host_pbs": host_pbs,
        "vm_locations": vm_locations,
        "host_error": host_error,
        "host_loading": host_loading,
        "configured": config is not None,
        "queue_paused": restore_config.queue_paused(),
        "action_error": request.query_params.get("error"),
        # Filter + pagination state for the runs log.
        "runs_total": total,
        "runs_page": page,
        "runs_pages": pages,
        "runs_per": per,
        "runs_per_options": _RUNS_PER_PAGE_OPTIONS,
        "runs_page_window": _page_window(page, pages),
        "runs_filter": {
            "f_status": f_status or "",
            "f_trigger": f_trigger or "",
            "f_cluster": f_cluster or "",
            "f_q": f_q or "",
        },
        "runs_has_filter": any([f_status, f_trigger, f_cluster, f_q]),
        "cluster_options": db_restore.run_filter_clusters(),
        # Active filter + page size as a query string (no page) so page links keep
        # both; per is omitted when it is the default to keep URLs clean.
        "runs_query": urlencode(
            {
                k: v
                for k, v in (
                    ("f_status", f_status),
                    ("f_trigger", f_trigger),
                    ("f_cluster", f_cluster),
                    ("f_q", f_q),
                    ("per", per if per != _RUNS_PER_PAGE_DEFAULT else None),
                )
                if v
            }
        ),
    }
    resp = templates.TemplateResponse(request, "restore.html", common_context(request) | context)
    # Remember an explicit page-size choice across visits (per-browser).
    if per_explicit is not None:
        resp.set_cookie(_PER_PAGE_COOKIE, str(per), max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@router.get("/restore/progress.json")
def restore_progress(request: Request) -> JSONResponse:
    """Live state of running restores, for the progress card's auto-refresh —
    the restore (and especially the slow post-copy volume rescan / cache flush)
    can run for minutes, so the card updates without a manual reload."""
    from app.i18n import t
    from app.services.human_format import format_size_coarse
    from app.web.common import restore_eta_text

    out = []
    for r in db_restore.list_runs(limit=20, status="running"):
        phase = r.get("phase") or "restoring"
        name = f"{r['cluster_name']}:{r['vm_name']}" if r.get("vm_name") else f"VM {r.get('vmid_src') or r['vm_id']}"
        progress = f"{format_size_coarse(r['bytes_done'])} {t('restore.read_label')}" if r.get("bytes_done") else ""
        if r.get("throughput_bps"):
            progress = f"{progress} · {format_size_coarse(r['throughput_bps'])}/s".strip(" ·")
        out.append(
            {
                "id": r["id"],
                "percent": r.get("percent") or 0,
                "phase": t(f"restore.phase_{phase}"),
                "name": name,
                "progress": progress,
                # Live remaining-time estimate (only while restoring); "" otherwise.
                "eta": restore_eta_text(r),
            }
        )
    # Carry the host I/O rates so the disk/network rings update live, reading the
    # same cached snapshot the page renders from (no SSH per poll) and nudging the
    # ~25 s background refresh when it is stale so the numbers keep moving.
    io = None
    host_state = None
    config = restore_config.load()
    if config is not None:
        with _host_view_lock:
            snap = dict(_host_view)
        if snap["data"] is None or (time.monotonic() - snap["at"]) > _HOST_VIEW_TTL:
            threading.Thread(target=_refresh_host_view, args=(config,), daemon=True).start()
        if snap["data"]:
            io = _io_view((snap["data"]["host"] or {}).get("io"))
        # Lets a page rendered while loading reload once the first refresh settles.
        host_state = "ready" if snap["data"] else ("error" if snap["error"] else "loading")
    return JSONResponse({"running": out, "io": io, "host": host_state})


def _io_view(io: dict | None) -> dict:
    """Shape the raw I/O rates for the live poll: arc percents plus display
    strings formatted exactly like the page's Jinja ``size_coarse`` filter, so a
    live update reads identically to the server-rendered ring."""
    from app.services.human_format import format_size_coarse as fsc

    if not io or not io.get("have"):
        return {"have": False}
    return {
        "have": True,
        "disk_read_pct": io["disk_read_pct"],
        "disk_write_pct": io["disk_write_pct"],
        "disk_ctr": f"{io['disk_util']}%",
        "disk_read": fsc(io["disk_read"]),
        "disk_write": fsc(io["disk_write"]),
        "net_in_pct": io["net_in_pct"],
        "net_out_pct": io["net_out_pct"],
        "net_ctr": f"{max(io['net_in_pct'], io['net_out_pct'])}%",
        "net_in": fsc(io["net_in"]),
        "net_out": fsc(io["net_out"]),
    }


@router.post("/restore/vm/{vm_id}/test")
def manual_test(request: Request, vm_id: int) -> RedirectResponse:
    from app.services import restore_match

    # Never queue a VM whose backups aren't matched on the restore host — confirm
    # eligibility first (same 1:1 match as auto). Only queue when confirmed.
    config = restore_config.load()
    if config is None:
        return RedirectResponse("/restore?error=not_configured", status_code=status.HTTP_303_SEE_OTHER)
    try:
        server = restore_orchestrator.build_server(config)
        entries = restore_orchestrator.restore_host_pbs_entries(server)
        matchable = restore_match.is_auto_restorable(restore_orchestrator.vm_backup_locations(vm_id), entries)
    except Exception as exc:
        logger.warning("manual test eligibility check failed for vm %s: %s", vm_id, exc)
        return RedirectResponse("/restore?error=host", status_code=status.HTTP_303_SEE_OTHER)
    if not matchable:
        return RedirectResponse("/restore?error=not_restorable", status_code=status.HTTP_303_SEE_OTHER)
    db_restore.create_run(vm_id, trigger="manual")
    try:
        from app.web.sync import _kick_restore_queue

        _kick_restore_queue()
    except Exception as exc:
        logger.debug("kick restore queue failed: %s", exc)
    return RedirectResponse("/restore", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/restore/runs/{run_id}/delete")
def delete_run(request: Request, run_id: int) -> RedirectResponse:
    """Remove a queued (planned) run from the queue."""
    db_restore.delete_run(run_id)
    return RedirectResponse("/restore", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/restore/runs/{run_id}/cancel")
def cancel_run(request: Request, run_id: int) -> RedirectResponse:
    """Ask the worker to abort a running run. It polls the flag in its long loops,
    stops the in-flight restore task, tears the environment (VM + helper) down
    cleanly, and records the run as cancelled."""
    db_restore.request_cancel(run_id)
    return RedirectResponse("/restore", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/restore/queue/pause")
def pause_queue(request: Request, paused: str = Form(default="1")) -> RedirectResponse:
    restore_config.set_queue_paused(paused == "1")
    return RedirectResponse("/restore", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/restore/queue/reorder")
def reorder_queue(order: list[int] = Form(default=[])) -> JSONResponse:
    """Persist a new manual order for the not-yet-started (queued) runs — applied
    immediately from the drag-reorder in the runs table (no save button). ``order``
    is the queued run ids in their new top-to-bottom sequence; runs that have since
    started are ignored."""
    updated = db_restore.reorder_queue(order)
    return JSONResponse({"ok": True, "updated": updated})


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
            # A held env has no helper (discarded to free its IP); re-provision a
            # fresh one, re-probe, and promote if it is green.
            result, fresh_plan = restore_orchestrator.reprovision_and_retest(run_id, run, config, server)
            if result == "passed":
                restore_worker.promote_held(run_id, fresh_plan, server)
    except Exception as exc:
        logger.warning("held action %s on run %s failed: %s", action, run_id, exc)


@router.post("/restore/vm/{vm_id}/policy")
def update_vm_restore_policy(
    request: Request,
    vm_id: int,
    auto_restore_enabled: str = Form(default=""),
    restore_cadence: str = Form(default="half_year"),
    probe_primary_ip: str = Form(default=""),
    probe_vlan: str = Form(default=""),
    probe_tcp_ports: str = Form(default=""),
    probe_require_icmp: str = Form(default=""),
    hold_on_failure: str = Form(default=""),
    restore_retry_days: int = Form(default=7),
    redirect: str = Form(default="/restore"),
    ajax: str = Form(default=""),
):
    ports = [int(p) for p in probe_tcp_ports.replace(",", " ").split() if p.strip().isdigit()]
    vlan = int(probe_vlan) if probe_vlan.strip().isdigit() else None
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO vm_policies
                (vm_id, owner_name, criticality, rpo_hours, auto_restore_enabled, restore_cadence,
                 probe_primary_ip, probe_vlan, probe_tcp_ports, probe_require_icmp, hold_on_failure, restore_retry_days)
            VALUES (%s, 'unassigned', 'medium', 24, %s, %s, NULLIF(%s, ''), %s, %s, %s, %s, %s)
            ON CONFLICT (vm_id) DO UPDATE SET
                auto_restore_enabled = EXCLUDED.auto_restore_enabled,
                restore_cadence = EXCLUDED.restore_cadence,
                probe_primary_ip = EXCLUDED.probe_primary_ip,
                probe_vlan = EXCLUDED.probe_vlan,
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
                vlan,
                ports,
                bool(probe_require_icmp),
                bool(hold_on_failure),
                restore_retry_days,
            ),
        )
        conn.commit()
    # Enabling auto on a VM that was never successfully restored should enter the
    # queue right now (not wait for the next sync tick, and not be skipped while a
    # restore is already running). Enqueue directly, then kick the drain.
    if auto_restore_enabled:
        try:
            db_restore.enqueue_if_never_passed(vm_id, trigger="scheduled")
        except Exception as exc:
            logger.debug("eager enqueue after policy save failed: %s", exc)
        try:
            from app.web.sync import _kick_restore_queue

            _kick_restore_queue()
        except Exception as exc:
            logger.debug("kick restore queue after policy save failed: %s", exc)
    if ajax == "1":
        from starlette.responses import Response

        return Response(status_code=204)  # auto-save: no navigation
    return RedirectResponse(redirect, status_code=status.HTTP_303_SEE_OTHER)
