"""Active PVE-Restore client: PVE API (write) + SSH (Paramiko).

This is the first *active* Proxmox integration (everything else stays
read-only). All destructive/SSH actions target only the configured scratch
host, gated by ``verify_host_identity`` and the target-storage interlock.

The PVE and SSH transports are injected (``PveClient`` / ``SshClient``
protocols) so the orchestration is unit-testable with fakes; the real
transports live at the bottom and import ``paramiko`` lazily. Pure decision
helpers (NIC rewrite, reaper selection, stall backstop, progress parsing) are
module functions and carry their own tests.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any, Protocol

logger = logging.getLogger(__name__)

OWNED_TAG = "bsentinel-restore"


class PveClient(Protocol):
    def get(self, path: str) -> Any: ...
    def post(self, path: str, data: dict[str, Any]) -> Any: ...
    def delete(self, path: str) -> Any: ...


class SshClient(Protocol):
    def run(self, command: str) -> str: ...


# --- pure helpers ------------------------------------------------------------


def rewrite_net_line(net_line: str, bridge: str, tag: int | None) -> str:
    """Rewrite a PVE ``netN`` config value to a new bridge (+ optional VLAN tag)
    while **preserving the MAC address and model** and dropping any old tag.

    ``virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0,tag=5`` → ``...,bridge=vmbr9,tag=901``
    """
    parts = [p for p in net_line.split(",") if p]
    kept = [p for p in parts if not p.startswith(("bridge=", "tag="))]
    kept.append(f"bridge={bridge}")
    if tag is not None:
        kept.append(f"tag={tag}")
    return ",".join(kept)


def select_reaper_targets(guests: list[dict[str, Any]], owned_vmids: set[int]) -> list[dict[str, Any]]:
    """Guests to destroy: everything on the scratch host that is NOT owned by a
    live (active/held) run. With no owned vmids, that is everything."""
    return [g for g in guests if g["vmid"] not in owned_vmids]


_PROGRESS_RE = re.compile(r"\bprogress\s+(\d+)%")
_BYTES_RE = re.compile(r"\((\d+(?:\.\d+)?)\s*([KMGT]i?B)\s*(?:of|/)\s*(\d+(?:\.\d+)?)\s*([KMGT]i?B)\)")
# Proxmox's own restore line: "progress 90% (read 30924603392 bytes, zeroes = …)".
_READ_RE = re.compile(r"read\s+(\d+)\s+bytes")
_UNIT = {
    "B": 1,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
}


def parse_task_progress(line: str) -> dict[str, Any] | None:
    """Extract ``{percent, bytes_done, bytes_total}`` from a restore task log
    line, or None if the line carries no progress."""
    pct = _PROGRESS_RE.search(line)
    out: dict[str, Any] = {}
    if pct:
        out["percent"] = int(pct.group(1))
    b = _BYTES_RE.search(line)
    if b:
        out["bytes_done"] = int(float(b.group(1)) * _UNIT.get(b.group(2), 1))
        out["bytes_total"] = int(float(b.group(3)) * _UNIT.get(b.group(4), 1))
    else:
        r = _READ_RE.search(line)
        if r:
            out["bytes_done"] = int(r.group(1))  # Proxmox gives bytes read, no total
    return out or None


def stall_exceeded(
    seconds_since_progress: float,
    percent_done: int,
    elapsed_seconds: float,
    factor: float,
    floor_seconds: float = 900.0,
) -> bool:
    """Adaptive stall backstop (spec §6): a quiet period only counts as a stall
    if it exceeds ``factor`` × the *expected time per 1%* (derived from progress
    so far), with a high floor. On a 36 TB guest where 1% legitimately takes
    >1 h, the allowed quiet time scales up accordingly, so a healthy slow restore
    is never aborted."""
    if percent_done <= 0:
        # No progress yet: allow at least the floor before suspecting a hang.
        return seconds_since_progress > max(floor_seconds, elapsed_seconds * factor)
    expected_per_pct = elapsed_seconds / percent_done
    allowed = max(floor_seconds, expected_per_pct * factor)
    return seconds_since_progress > allowed


# --- the active client -------------------------------------------------------


class HostIdentityError(RuntimeError):
    """The configured scratch host matches a known production endpoint."""


class RestoreServer:
    def __init__(self, config: dict[str, Any], pve: PveClient, ssh: SshClient) -> None:
        self.config = config
        self.pve = pve
        self.ssh = ssh
        self._node: str | None = None

    # host identity / safety -------------------------------------------------

    def verify_host_identity(self, production_hosts: set[str]) -> None:
        """Refuse to operate if the scratch host resolves to any known
        production cluster/PBS endpoint (spec §6 interlock)."""
        ours = {self.config.get("ssh_host", "").lower(), _host_of(self.config.get("api_url", ""))}
        clash = ours & {h.lower() for h in production_hosts}
        if clash:
            raise HostIdentityError(f"restore host overlaps production endpoint(s): {sorted(clash)}")

    def node(self) -> str:
        if self._node is None:
            nodes = self.pve.get("/nodes") or []
            online = [n for n in nodes if n.get("status") == "online"] or nodes
            self._node = online[0]["node"]
        return self._node

    # capacity ---------------------------------------------------------------

    def free_resources(self) -> dict[str, Any]:
        node = self.node()
        status = self.pve.get(f"/nodes/{node}/status") or {}
        mem = status.get("memory", {})
        cfg_targets = self.config.get("target_storages", [])
        classes = {s["storage"]: s.get("disk_class") for s in cfg_targets}
        # Operator's configured preference order, so the UI is stable across
        # reloads instead of following PVE's arbitrary listing order.
        orders = {s["storage"]: s.get("order", 9999) for s in cfg_targets}
        # Only the operator's selected (ticked) target storages are shown as rings.
        selected = {s["storage"] for s in cfg_targets if s.get("use", True)}
        stores = []
        for s in self.pve.get(f"/nodes/{node}/storage") or []:
            if s.get("storage") not in selected:
                continue
            content = s.get("content", "") or ""
            images = "images" in content  # only image-capable storages are restore targets
            total = s.get("total")
            avail = s.get("avail")
            used = s.get("used")
            if used is None and total is not None and avail is not None:
                used = total - avail
            stores.append(
                {
                    "storage": s["storage"],
                    "type": s.get("type", ""),
                    "images": images,
                    "used": used,
                    "total": total,
                    "avail": avail,
                    # Default image-capable storages to standard-ssd so a disk-type
                    # icon always shows (same as the settings table) until assigned.
                    "disk_class": classes.get(s["storage"]) or ("standard-ssd" if images else None),
                    "order": orders.get(s["storage"], 9999),
                }
            )
        stores.sort(key=lambda s: (s["order"], s["storage"]))
        return {"ram_free": mem.get("free"), "ram_total": mem.get("total"), "stores": stores}

    def list_pbs_storages(self) -> list[dict[str, Any]]:
        """The restore host's PBS storages with the fields needed to resolve their
        identity: ``server``/``datastore``/``namespace``/``fingerprint``. Read from
        the datacenter storage *config* (``/storage``), which carries these — the
        node status endpoint (``/nodes/{node}/storage``) does not."""
        out = []
        for s in self.pve.get("/storage") or []:
            if s.get("type") == "pbs":
                out.append(
                    {
                        "storage": s["storage"],
                        "server": s.get("server", ""),
                        "datastore": s.get("datastore", ""),
                        "namespace": s.get("namespace", ""),
                        "fingerprint": s.get("fingerprint", ""),
                    }
                )
        return out

    def list_snapshots(self, pbs_storage: str, src_vmid: int) -> list[dict[str, Any]]:
        """Backups of ``src_vmid`` in a local PBS storage, newest-first candidates
        for restore. PVE lists only finished snapshots, so each counts as
        complete (completeness, not verification; spec §3, §7)."""
        node = self.node()
        content = self.pve.get(f"/nodes/{node}/storage/{pbs_storage}/content?content=backup&vmid={src_vmid}") or []
        out = []
        for vol in content:
            out.append(
                {
                    "snapshot_id": vol.get("volid", ""),
                    "backup_time": vol.get("ctime", 0),
                    "size": vol.get("size"),
                    "complete": True,
                }
            )
        return out

    def list_target_storages(self) -> dict[str, Any]:
        """Enumerate storages and split out the non-PBS restore targets. Queries
        both the node storage status and the datacenter storage config and merges
        them. Returns ``{"targets": [...], "total": int, "pbs": int}`` so the
        caller can tell apart "no storages at all / unreachable" from "only PBS
        source datastores are visible" (the token lacks Datastore.Audit on the
        regular target storages — the usual cause of an empty target list)."""
        seen: dict[str, dict[str, Any]] = {}
        all_names: set[str] = set()
        pbs = 0
        errors: list[str] = []
        paths = ["/storage"]
        try:
            paths.insert(0, f"/nodes/{self.node()}/storage")
        except Exception as exc:
            errors.append(f"/nodes: {exc}")
        for path in paths:
            try:
                rows = self.pve.get(path) or []
            except Exception as exc:
                errors.append(f"{path}: {str(exc)[:120]}")
                continue
            logger.info(
                "restore-server %s → %d storage(s): %s",
                path,
                len(rows),
                ", ".join(f"{s.get('storage')}({s.get('type')})" for s in rows) or "(none)",
            )
            for s in rows:
                name = s.get("storage")
                if not name:
                    continue
                all_names.add(name)
                if s.get("type") == "pbs":
                    pbs += 1
                    continue
                if name in seen:
                    continue
                content = s.get("content", "") or ""
                seen[name] = {
                    "storage": name,
                    "type": s.get("type", ""),
                    "content": content,
                    "images": "images" in content,
                    # Usage is only on the node-status rows (first path), not the
                    # datacenter config; whichever path saw it first wins.
                    "used": s.get("used"),
                    "total": s.get("total"),
                    "avail": s.get("avail"),
                }
        # Nothing readable AND every source errored → surface it (don't masquerade
        # as "no storages found").
        if not all_names and errors:
            raise RuntimeError("; ".join(errors))
        return {"targets": list(seen.values()), "total": len(all_names), "pbs": pbs}

    # sandbox network --------------------------------------------------------

    def ensure_sandbox_bridge(self) -> None:
        """Idempotently ensure a VLAN-aware, portless bridge exists (spec §6)."""
        bridge = self.config["sandbox_bridge"]
        script = (
            f"grep -q 'iface {bridge}' /etc/network/interfaces /etc/network/interfaces.d/* 2>/dev/null || "
            f"(printf 'auto {bridge}\\niface {bridge} inet manual\\n\\tbridge-ports none\\n"
            f"\\tbridge-stp off\\n\\tbridge-fd 0\\n\\tbridge-vlan-aware yes\\n\\tbridge-vids 2-4094\\n' "
            f">> /etc/network/interfaces.d/{bridge}.cfg && ifup {bridge})"
        )
        self.ssh.run(script)

    # restore pipeline -------------------------------------------------------

    def restore(
        self,
        restore_vmid: int,
        source: dict[str, Any],
        snapshot_id: str,
        target_storage: str,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        on_task: Callable[[str], None] | None = None,
        *,
        poll_interval: float = 5.0,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Restore a snapshot to ``restore_vmid`` on the target storage, tracking
        live progress. Liveness is the task status; abort only on task error or
        an adaptive stall (spec §6, §11). ``on_task`` receives the PVE task UPID
        as soon as it starts, so the caller can persist it and re-attach after an
        app restart (a deploy must not interrupt a running restore)."""
        node = self.node()
        # snapshot_id is the PBS volume id (e.g. "store:backup/vm/100/2026-...Z"),
        # which PVE accepts directly as the restore archive.
        archive = (
            snapshot_id if ":" in snapshot_id else f"{source['storage']}:backup/vm/{source['src_vmid']}/{snapshot_id}"
        )
        upid = self.pve.post(
            f"/nodes/{node}/qemu",
            {"vmid": restore_vmid, "archive": archive, "storage": target_storage, "live-restore": 0},
        )
        if on_task:
            on_task(upid)
        self._poll_task(node, upid, on_progress, poll_interval=poll_interval, now=now, sleep=sleep)

    def poll_task(
        self,
        upid: str,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        *,
        poll_interval: float = 5.0,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Re-attach to an existing PVE task (by UPID) and poll it to completion —
        used to resume a restore whose app process was restarted mid-run. A task
        that already finished returns at once; a failed one raises RestoreTaskError."""
        self._poll_task(self.node(), upid, on_progress, poll_interval=poll_interval, now=now, sleep=sleep)

    def _poll_task(
        self,
        node: str,
        upid: str,
        on_progress: Callable[[dict[str, Any]], None] | None,
        *,
        poll_interval: float,
        now: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        start = now()
        last_progress_at = start
        last_percent = 0
        seen_log_lines = 0
        factor = float(self.config.get("stall_backstop_factor") or 6.0)
        while True:
            try:
                status = self.pve.get(f"/nodes/{node}/tasks/{upid}/status") or {}
                # Page from the last line seen: the task-log API caps at ~50 lines
                # per call, so without `start` the progress froze at the first page.
                log = self.pve.get(f"/nodes/{node}/tasks/{upid}/log?start={seen_log_lines}&limit=500") or []
            except Exception as exc:
                logger.warning("task poll blip (continuing): %s", exc)
                sleep(poll_interval)
                if stall_exceeded(now() - last_progress_at, last_percent, now() - start, factor):
                    raise RestoreTaskError("restore task unreachable past the adaptive backstop") from exc
                continue
            for entry in log:
                prog = parse_task_progress(entry.get("t", "") if isinstance(entry, dict) else str(entry))
                if prog:
                    last_progress_at = now()
                    last_percent = prog.get("percent", last_percent)
                    if on_progress:
                        on_progress(prog)
            seen_log_lines += len(log)

            if status.get("status") == "stopped":
                if status.get("exitstatus") not in (None, "OK"):
                    raise RestoreTaskError(f"restore task failed: {status.get('exitstatus')}")
                return

            if stall_exceeded(now() - last_progress_at, last_percent, now() - start, factor):
                raise RestoreTaskError("restore task stalled (no progress; adaptive backstop exceeded)")
            sleep(poll_interval)

    def create_api_token(self, user: str = "root@pam", token_id: str = "b-sentinel") -> dict[str, str]:
        """Create a PVE API token over SSH and return its credentials. Uses
        ``pvesh`` (reliable JSON) with ``privsep 0`` so the token inherits the
        user's full permissions — no ACLs to maintain, all storages visible.

        Non-destructive: we try to create first and only delete+recreate when the
        token already exists, so a failure never leaves the host without a token.
        Safe because we already hold root SSH on this dedicated host."""
        path = f"/access/users/{user}/token/{token_id}"
        add_cmd = f"pvesh create {path} --privsep 0 --output-format json"
        try:
            out = self.ssh.run(add_cmd)
        except Exception as first:
            logger.info("token create failed once (%s); deleting + retrying", str(first)[:140])
            try:
                self.ssh.run(f"pvesh delete {path}")
            except Exception as exc:
                logger.debug("token delete (ignored): %s", exc)
            out = self.ssh.run(add_cmd)
        data = _extract_json(out)
        secret = data.get("value") or (data.get("info") or {}).get("value")
        logger.info("provisioned API token %s!%s (secret length %d)", user, token_id, len(secret or ""))
        if not secret:
            raise RuntimeError(f"token created but no secret in output: {out[:200]}")
        return {"api_user": user, "token_id": token_id, "token_secret": secret}

    def exists(self, vmid: int, *, is_lxc: bool = False) -> bool:
        """Whether a guest with this vmid is present on the host — so teardown
        doesn't blindly shut down / destroy a VM that was never created (e.g. a
        run that errored before restoring)."""
        if not vmid:
            return False
        kind = "lxc" if is_lxc else "qemu"
        try:
            guests = self.pve.get(f"/nodes/{self.node()}/{kind}") or []
        except Exception:
            return False
        return any(int(g.get("vmid", -1)) == int(vmid) for g in guests)

    def rewrite_nics(self, vmid: int, vlan_tag: int) -> None:
        """Re-point every NIC to the sandbox bridge + this env's VLAN tag,
        keeping MACs (spec §10)."""
        node = self.node()
        config = self.pve.get(f"/nodes/{node}/qemu/{vmid}/config") or {}
        bridge = self.config["sandbox_bridge"]
        changes = {}
        for key, value in config.items():
            if re.fullmatch(r"net\d+", key) and isinstance(value, str):
                changes[key] = rewrite_net_line(value, bridge, vlan_tag)
        if changes:
            self.pve.post(f"/nodes/{node}/qemu/{vmid}/config", changes)

    def start(self, vmid: int) -> None:
        self.pve.post(f"/nodes/{self.node()}/qemu/{vmid}/status/start", {})

    def shutdown(self, vmid: int, *, force_after: bool = True) -> None:
        node = self.node()
        try:
            self.pve.post(f"/nodes/{node}/qemu/{vmid}/status/shutdown", {"timeout": 60})
        except Exception:
            if force_after:
                self.pve.post(f"/nodes/{node}/qemu/{vmid}/status/stop", {})

    def destroy(self, vmid: int, *, is_lxc: bool = False) -> None:
        kind = "lxc" if is_lxc else "qemu"
        self.pve.delete(f"/nodes/{self.node()}/{kind}/{vmid}?purge=1&destroy-unreferenced-disks=1")

    # reaper -----------------------------------------------------------------

    def _lxc_storage(self) -> str:
        """The storage the helper container's rootfs goes on: the first selected
        target storage, in the operator's order, that accepts LXC rootfs (content
        'rootdir'). An images-only target (fine for a VM disk, not a container) is
        skipped — so the set may differ from the VM restore target. No separate
        setting; it's derived from the same target-storage selection."""
        node = self.node()
        content = {s.get("storage"): (s.get("content") or "") for s in (self.pve.get(f"/nodes/{node}/storage") or [])}
        selected = sorted(
            (s for s in self.config.get("target_storages", []) if s.get("use", True)),
            key=lambda s: s.get("order", 9999),
        )
        for t in selected:
            if "rootdir" in content.get(t["storage"], ""):
                return t["storage"]
        return ""

    def _template_volid(self) -> str:
        """The configured helper template as a storage VOLUME ID (what a non-root
        API token may pass). A volid is used as-is; a filesystem path is matched
        by filename against the host's listed CT templates, so the operator can
        configure either form. "" if nothing matches."""
        want = str(self.config.get("helper_template", "")).strip()
        if want and ":" in want and not want.startswith("/"):
            return want  # already a storage volume id
        base = want.rsplit("/", 1)[-1]
        if not base:
            return ""
        node = self.node()
        for s in self.pve.get(f"/nodes/{node}/storage") or []:
            if "vztmpl" not in (s.get("content") or ""):
                continue
            for vol in self.pve.get(f"/nodes/{node}/storage/{s['storage']}/content?content=vztmpl") or []:
                if str(vol.get("volid", "")).rsplit("/", 1)[-1] == base:
                    return vol["volid"]
        return ""

    def provision_helper(
        self,
        helper_vmid: int,
        vlan_tag: int,
        mgmt_ip: str,
        reservations: list[dict[str, Any]],
    ) -> None:
        """Create + start the dual-homed helper LXC: sandbox leg (this env's VLAN
        tag) running dnsmasq, management leg (static mgmt IP) for reachability.
        The helper never forwards between legs (spec §10). Its rootfs goes on the
        first selected target storage that accepts containers — no separate
        storage to configure."""
        import json

        node = self.node()
        cfg = self.config
        helper_storage = self._lxc_storage()
        if not helper_storage:
            raise RuntimeError("no selected target storage accepts LXC containers (content 'rootdir')")
        bridge = cfg["sandbox_bridge"]
        mgmt_prefix = _prefix_from_netmask(cfg["mgmt_netmask"])
        probe_offset = int(cfg.get("probe_offset", 254))
        sandbox_aliases = helper_subnet_addrs(reservations, probe_offset)
        net_ifaces, alias_addrs = helper_legs(
            bridge,
            vlan_tag,
            cfg["mgmt_iface"],
            mgmt_ip,
            mgmt_prefix,
            cfg.get("mgmt_vlan"),
            cfg.get("mgmt_gateway", ""),
            sandbox_aliases,
            reservations,
        )
        # All via the API token: a non-root token may pass an ostemplate that is a
        # storage VOLUME ID, just not an absolute filesystem path ("Only root can
        # pass arbitrary filesystem paths"). So resolve the template to its volid
        # first; rootfs goes on the selected container-capable target storage.
        template = self._template_volid()
        if not template:
            raise RuntimeError(
                "helper template not found as a storage volume on the restore host "
                "(set restore_server.helper_template to a volid like local:vztmpl/alpine-…tar.zst)"
            )
        self.pve.post(
            f"/nodes/{node}/lxc",
            {
                "vmid": helper_vmid,
                "ostemplate": template,
                "storage": helper_storage,
                "hostname": f"{OWNED_TAG}-helper-{helper_vmid}",
                "memory": 256,
                "cores": 1,
                **net_ifaces,
                "tags": OWNED_TAG,
                "start": 0,
            },
        )
        self.start_lxc(helper_vmid)
        # No forwarding between legs; load dnsmasq + probe agent.
        self.ssh.run(f"pct exec {helper_vmid} -- sysctl -w net.ipv4.ip_forward=0")
        from app.services.restore_probe import PROBE_AGENT_SCRIPT

        conf = dnsmasq_config(reservations, probe_offset)
        self._write_file(helper_vmid, "/etc/dnsmasq.d/restore.conf", conf)
        self._write_file(helper_vmid, "/opt/probe.py", PROBE_AGENT_SCRIPT)
        for extra in alias_addrs:
            self.ssh.run(f"pct exec {helper_vmid} -- ip addr add {extra['ip']}/{extra['prefix']} dev sbx")
        self.ssh.run(
            f"pct exec {helper_vmid} -- rc-service dnsmasq restart || pct exec {helper_vmid} -- "
            f"systemctl restart dnsmasq"
        )
        _ = json  # reserved for future structured push

    def start_lxc(self, vmid: int) -> None:
        self.pve.post(f"/nodes/{self.node()}/lxc/{vmid}/status/start", {})

    def _write_file(self, helper_vmid: int, path: str, content: str) -> None:
        import base64

        b64 = base64.b64encode(content.encode()).decode()
        self.ssh.run(f"pct exec {helper_vmid} -- sh -c 'echo {b64} | base64 -d > {path}'")

    def probe(self, helper_vmid: int, targets: list[dict[str, Any]]) -> dict[str, Any]:
        """Run the probe agent inside the helper (host-mediated via pct exec) and
        return its structured JSON result."""
        import json

        req = json.dumps({"targets": targets})
        b64 = __import__("base64").b64encode(req.encode()).decode()
        out = self.ssh.run(f"echo {b64} | base64 -d | pct exec {helper_vmid} -- python3 /opt/probe.py")
        return json.loads(out) if out.strip() else {}

    def reap(self, owned_vmids: set[int]) -> list[int]:
        """Destroy every guest on the scratch host not owned by a live run
        (spec §12). Returns the destroyed vmids."""
        node = self.node()
        destroyed = []
        for kind in ("qemu", "lxc"):
            guests = self.pve.get(f"/nodes/{node}/{kind}") or []
            for g in select_reaper_targets(guests, owned_vmids):
                self.destroy(g["vmid"], is_lxc=(kind == "lxc"))
                destroyed.append(g["vmid"])
        return destroyed


class RestoreTaskError(RuntimeError):
    pass


def _host_of(url: str) -> str:
    m = re.search(r"https?://([^:/]+)", url or "")
    return m.group(1).lower() if m else ""


def _prefix_from_netmask(netmask: str) -> int:
    import ipaddress

    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError):
        return 24


def dnsmasq_config(reservations: list[dict[str, Any]], probe_offset: int) -> str:
    """Build a dnsmasq config that hands each VM NIC its angestammte IP by MAC
    and serves one dhcp-range per distinct subnet (spec §10). ``reservations``
    are ``{mac, ip, prefix}``; the helper's own address in each subnet is the
    ``probe_offset`` host number (collision-checked against the VM's IPs)."""
    import ipaddress

    lines = ["# generated by backup-sentinel restore helper", "bind-interfaces", "except-interface=lo", "no-resolv"]
    subnets: dict[str, int] = {}
    for r in reservations:
        net = ipaddress.ip_network(f"{r['ip']}/{r['prefix']}", strict=False)
        subnets[str(net)] = r["prefix"]
        lines.append(f"dhcp-host={r['mac']},{r['ip']},infinite")
    for cidr in subnets:
        net = ipaddress.ip_network(cidr)
        helper_ip = str(net.network_address + probe_offset)
        # Static range = just the helper's own address; all guests use reservations.
        lines.append(f"dhcp-range={helper_ip},{helper_ip},static,infinite")
    return "\n".join(lines) + "\n"


def helper_subnet_addrs(reservations: list[dict[str, Any]], probe_offset: int) -> list[dict[str, Any]]:
    """The helper's own IP (offset host) in each distinct VM subnet, for aliasing
    its sandbox leg so it can probe every subnet."""
    import ipaddress

    seen: dict[str, dict[str, Any]] = {}
    for r in reservations:
        net = ipaddress.ip_network(f"{r['ip']}/{r['prefix']}", strict=False)
        helper_ip = str(net.network_address + probe_offset)
        if helper_ip in {x["ip"] for x in seen.values()} or helper_ip in {rr["ip"] for rr in reservations}:
            helper_ip = str(net.network_address + max(1, probe_offset - 1))
        seen[str(net)] = {"ip": helper_ip, "prefix": r["prefix"]}
    return list(seen.values())


def guest_in_mgmt_subnet(reservations: list[dict[str, Any]], mgmt_ip: str, mgmt_prefix: int = 24) -> bool:
    """True when a restored guest NIC sits in the same subnet as the helper
    management IPs. Then one leg suffices: that leg is in the guest's subnet (to
    serve DHCP + probe) and is the management address at the same time."""
    import ipaddress

    try:
        mgmt_net = ipaddress.ip_network(f"{mgmt_ip}/{mgmt_prefix}", strict=False)
    except ValueError:
        return False
    for r in reservations:
        try:
            if ipaddress.ip_address(r["ip"]) in mgmt_net:
                return True
        except (ValueError, KeyError):
            continue
    return False


def helper_legs(
    bridge: str,
    vlan_tag: int,
    mgmt_iface: str,
    mgmt_ip: str,
    mgmt_prefix: int,
    mgmt_vlan: int | None,
    mgmt_gateway: str,
    sandbox_aliases: list[dict[str, Any]],
    reservations: list[dict[str, Any]],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Decide the helper's network legs. Returns (net_ifaces, extra_alias_addrs).

    Dual-homed (default): net0 on the sandbox VLAN (serves the guest) + net1 on
    the management interface (worker reachability); extra sandbox subnets aliased.

    Single-homed (spec §10 special case): when a guest NIC is already in the
    management subnet, one leg on the sandbox VLAN carries the management IP and
    serves both roles — no separate management leg. All sandbox offset addresses
    are aliased so dnsmasq has its per-subnet address."""
    if guest_in_mgmt_subnet(reservations, mgmt_ip, mgmt_prefix):
        net0 = f"name=sbx,bridge={bridge},tag={vlan_tag},ip={mgmt_ip}/{mgmt_prefix}"
        return {"net0": net0}, list(sandbox_aliases)
    mgmt_net = f"name=mgmt,bridge={mgmt_iface},ip={mgmt_ip}/{mgmt_prefix}"
    if mgmt_gateway:
        mgmt_net += f",gw={mgmt_gateway}"
    if mgmt_vlan:
        mgmt_net += f",tag={mgmt_vlan}"
    first = sandbox_aliases[0] if sandbox_aliases else {"ip": "0.0.0.0", "prefix": 24}
    net0 = f"name=sbx,bridge={bridge},tag={vlan_tag},ip={first['ip']}/{first['prefix']}"
    return {"net0": net0, "net1": mgmt_net}, list(sandbox_aliases[1:])


# --- real transports (paramiko imported lazily) ------------------------------


class PveApi:
    """Real PVE API transport (token auth) for the restore host."""

    def __init__(self, api_url: str, api_user: str, token_id: str, token_secret: str) -> None:
        from app.services.proxmox import build_token_header

        self.base = api_url.rstrip("/") + "/api2/json"
        self.auth = build_token_header(api_user, token_id, token_secret)

    def get(self, path: str) -> Any:
        from app.services.api_client import api_get

        # Fail fast: a single scratch host, and the long-poll loop handles
        # transient blips itself (no API-layer retries stacking delays on the UI).
        return api_get(self.base, path, self.auth, label="PVE-Restore", retries=0)

    def post(self, path: str, data: dict[str, Any]) -> Any:
        from app.services.api_client import api_send

        return api_send("POST", self.base, path, self.auth, data=data, label="PVE-Restore")

    def delete(self, path: str) -> Any:
        from app.services.api_client import api_send

        return api_send("DELETE", self.base, path, self.auth, label="PVE-Restore")


def _extract_json(text: str) -> dict[str, Any]:
    """Parse the first JSON object in ``text`` (CLI tools may prepend warnings)."""
    import json

    s = text.strip()
    try:
        return json.loads(s)
    except ValueError:
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end > start:
            return json.loads(s[start : end + 1])
        raise RuntimeError(f"no JSON in command output: {text[:200]}") from None


def scan_host_key(host: str, port: int = 22, timeout: float = 10.0) -> str:
    """Read the host's SSH public key once (trust-on-first-use) and return it as
    a known_hosts-style line '<host> <type> <base64>'. The caller pins the result
    so later connections verify against it (RejectPolicy) and a changed key is
    rejected — safer than accepting an unknown key on every connect."""
    import socket

    import paramiko

    sock = socket.create_connection((host, port), timeout=timeout)
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        return f"{host} {key.get_name()} {key.get_base64()}"
    finally:
        transport.close()


def load_private_key(key_str: str) -> Any:
    """Parse an SSH private key of any common type (Ed25519, ECDSA, RSA; PEM or
    the newer OpenSSH format). Paramiko's per-class loaders raise obscure errors
    like 'unpack requires a buffer of 4 bytes' when handed the wrong type, so we
    try each in turn."""
    import io

    import paramiko

    errors = []
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(io.StringIO(key_str))
        except Exception as exc:
            errors.append(f"{cls.__name__}: {exc}")
    raise ValueError("unsupported or invalid SSH private key (" + "; ".join(errors) + ")")


def add_host_key(client: Any, host: str, host_key_line: str) -> None:
    """Pin the scratch host's SSH host key from a configured known_hosts-style
    line (e.g. from `ssh-keyscan`: '<host> ssh-ed25519 AAAA...'), so RejectPolicy
    can verify it without a system known_hosts file."""
    import base64

    import paramiko

    parts = host_key_line.split()
    # Accept either "<host> <type> <base64>" or just "<type> <base64>".
    if len(parts) >= 3:
        key_type, b64 = parts[1], parts[2]
    elif len(parts) == 2:
        key_type, b64 = parts[0], parts[1]
    else:
        raise ValueError("SSH host key must be a 'ssh-... AAAA...' line")
    key = paramiko.PKey.from_type_string(key_type, base64.b64decode(b64))
    client.get_host_keys().add(host, key_type, key)


class ParamikoSsh:
    def __init__(self, host: str, port: int, user: str, private_key: str, host_key: str = "") -> None:
        self._args = (host, port, user, private_key)
        self._host_key = host_key
        self._client: Any = None

    def _connect(self) -> Any:
        import paramiko

        host, port, user, key_str = self._args
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.load_system_host_keys()
        if self._host_key.strip():
            add_host_key(client, host, self._host_key.strip())
        pkey = load_private_key(key_str)
        client.connect(host, port=port, username=user, pkey=pkey, timeout=15)
        return client

    def run(self, command: str) -> str:
        if self._client is None:
            self._client = self._connect()
        _stdin, stdout, stderr = self._client.exec_command(command, timeout=120)
        out = stdout.read().decode()
        err = stderr.read().decode()
        if stdout.channel.recv_exit_status() != 0:
            raise RuntimeError(f"ssh command failed: {err.strip() or out.strip()}")
        return out
