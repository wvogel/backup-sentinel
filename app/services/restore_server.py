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

import json
import logging
import re
import shlex
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlsplit

logger = logging.getLogger(__name__)

OWNED_TAG = "bsentinel-restore"
HELPER_IMAGE_NAME = "restore-helper.tar.gz"  # the app-built helper CT template


class PveClient(Protocol):
    def get(self, path: str) -> Any: ...
    def post(self, path: str, data: dict[str, Any]) -> Any: ...
    def delete(self, path: str) -> Any: ...
    def upload(self, node: str, storage: str, filename: str, data: bytes) -> Any: ...


class SshClient(Protocol):
    def run(self, command: str) -> str: ...
    def put_bytes(self, remote_path: str, data: bytes) -> None: ...


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


_DRIVE_KEY = re.compile(r"(ide|sata|scsi|virtio)\d+")


def eject_cdroms(config: dict[str, Any]) -> dict[str, str]:
    """Config changes that let a restored guest boot in the isolated sandbox:
    eject every CD-ROM that still points at a backing volume (an ISO on the
    SOURCE cluster's storage, e.g. its NAS). That storage doesn't exist on the
    restore host, so ``VM ... Start`` fails with "storage '…' does not exist"
    until the drive is emptied. A restore test boots from disk and never needs
    the ISO, so each such drive is set to ``none,media=cdrom``."""
    changes: dict[str, str] = {}
    for key, value in config.items():
        if not isinstance(value, str) or not _DRIVE_KEY.fullmatch(key):
            continue
        if "media=cdrom" not in value:
            continue
        first = value.split(",", 1)[0].strip()
        if first and first not in ("none", "cdrom"):
            changes[key] = "none,media=cdrom"
    return changes


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


def split_path_query(path: str) -> tuple[str, dict[str, str]]:
    """Split a REST path with an optional query string into (path, params) — so a
    query like ``/…/content?content=vztmpl&vmid=100`` becomes pvesh ``-content``
    / ``-vmid`` options."""
    parts = urlsplit(path)
    return parts.path, dict(parse_qsl(parts.query))


def pvesh_command(verb: str, path: str, params: dict[str, Any] | None = None) -> str:
    """Build a shell-quoted ``pvesh`` command that mirrors a REST call. ``verb`` is
    get/create/set/delete (POST→create, PUT→set). None-valued params are dropped;
    everything is JSON so callers parse a single, predictable format."""
    args = ["pvesh", verb, path]
    for key, value in (params or {}).items():
        if value is None:
            continue
        args += [f"-{key}", str(value)]
    args += ["--output-format", "json"]
    return " ".join(shlex.quote(a) for a in args)


def parse_pvesh_output(out: str) -> Any:
    """Parse ``pvesh … --output-format json`` output: a JSON value, or None when
    empty. A bare non-JSON line (some pvesh create calls print a raw UPID) is
    returned verbatim so task polling still gets its UPID string."""
    text = (out or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def classify_task_error(exitstatus: str, log_tail: str = "") -> str:
    """Human message for a failed restore task. Proxmox exits 255 for almost
    everything with the real reason on the stderr lines *above* the "command …
    failed" line, so we match those (``log_tail``) — not the command line, which
    always carries ``--crypt-mode=encrypt`` for encrypted backups and would
    false-positive. Recognises a wrong PBS encryption key, missing permission,
    cert, or connectivity; otherwise falls back to the raw detail."""
    tail = (log_tail or "").lower()
    hint = ""
    if "decrypt" in tail or "encryption key" in tail or "wrong key" in tail:
        hint = (
            "backup decryption failed — the PBS encryption key on the restore host does not match "
            "this backup's key (check the datastore/namespace key)"
        )
    elif any(k in tail for k in ("permission", "access denied", "not allowed", "403 forbidden", "no permission")):
        hint = "permission denied reading the backup — check the PBS user's rights on this datastore/namespace"
    elif any(k in tail for k in ("fingerprint", "certificate verify", "self signed", "self-signed", "tls error")):
        hint = "PBS server certificate could not be verified (fingerprint/cert mismatch)"
    elif any(k in tail for k in ("connection refused", "timed out", "no route to host", "could not resolve")):
        hint = "could not reach the PBS server (connection/DNS)"
    detail = (log_tail.strip() or str(exitstatus).strip())[:600]
    return f"{hint}: {detail}" if hint else f"restore task failed: {detail}"


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
    def __init__(
        self, config: dict[str, Any], pve: PveClient, ssh: SshClient, pve_root: PveClient | None = None
    ) -> None:
        self.config = config
        # Hybrid transport: `pve` is the fast API token (reads + token-safe writes
        # — status/log polling, node/storage, NICs, start/stop/destroy, helper).
        # `pve_root` is pvesh over root SSH, used ONLY for the two things a token
        # may not do: the restore itself (qmrestore sets hostpci) and deleting
        # hostpci before the start. Defaults to `pve` so tests inject one fake.
        self.pve = pve
        self.pve_root = pve_root or pve
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
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        """Restore a snapshot to ``restore_vmid`` on the target storage, tracking
        live progress. Liveness is the task status; abort only on task error, an
        adaptive stall (spec §6, §11), or an operator cancel. ``on_task`` receives
        the PVE task UPID as soon as it starts, so the caller can persist it and
        re-attach after an app restart (a deploy must not interrupt a running
        restore)."""
        node = self.node()
        # snapshot_id is the PBS volume id (e.g. "store:backup/vm/100/2026-...Z"),
        # which PVE accepts directly as the restore archive.
        archive = (
            snapshot_id if ":" in snapshot_id else f"{source['storage']}:backup/vm/{source['src_vmid']}/{snapshot_id}"
        )
        # Start the restore as ROOT (a token can't apply a backed-up hostpci) but
        # in the BACKGROUND, returning the task UPID immediately — then poll that
        # UPID over the FAST API token for live progress. (Running `pvesh create`
        # in the foreground blocks until the whole restore finishes, which froze
        # the progress at 0% the entire time.)
        upid = self._start_restore_task(restore_vmid, archive, target_storage, node, now=now, sleep=sleep)
        if on_task:
            on_task(upid)
        self._poll_task(
            node, upid, on_progress, poll_interval=poll_interval, now=now, sleep=sleep, should_cancel=should_cancel
        )

    def _start_restore_task(
        self,
        vmid: int,
        archive: str,
        storage: str,
        node: str,
        *,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        deadline_seconds: float = 120.0,
    ) -> str:
        """Kick the restore as root in the background (so hostpci is applied) and
        return its PVE task UPID as soon as it appears — without waiting for the
        restore to finish, so we can poll it live over the fast API. The UPID is
        found via the API task list (robust; no parsing of pvesh's stdout, which
        block-buffers and only flushed at the very end — freezing progress at 0%
        until completion)."""
        out = f"/tmp/bsentinel-restore-{vmid}.log"
        self.ssh.run(
            f"nohup pvesh create /nodes/{shlex.quote(node)}/qemu "
            f"-vmid {vmid} -archive {shlex.quote(archive)} -storage {shlex.quote(storage)} -live-restore 0 "
            f"> {shlex.quote(out)} 2>&1 & echo kicked"
        )
        deadline = now() + deadline_seconds
        while now() < deadline:
            for tk in self.pve.get(f"/nodes/{node}/tasks?vmid={vmid}&limit=50") or []:
                upid = tk.get("upid")
                if str(tk.get("id")) == str(vmid) and "restore" in str(tk.get("type") or "").lower() and upid:
                    logger.info("restore task for VM %s: %s", vmid, upid)
                    return upid
            sleep(2.0)
        tail = ""
        try:
            tail = self.ssh.run(f"tail -c 400 {shlex.quote(out)} 2>/dev/null || true").strip()
        except Exception as exc:
            logger.debug("restore log tail failed: %s", exc)
        raise RestoreTaskError(f"restore task for VM {vmid} did not appear; log: {tail[:300]}")

    def stop_task(self, upid: str, node: str | None = None) -> None:
        """Ask PVE to stop a running task (used to abort an in-flight restore on
        cancel). Best-effort; the caller tears the environment down afterwards."""
        self.pve.delete(f"/nodes/{node or self.node()}/tasks/{upid}")

    def poll_task(
        self,
        upid: str,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        *,
        poll_interval: float = 5.0,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        """Re-attach to an existing PVE task (by UPID) and poll it to completion —
        used to resume a restore whose app process was restarted mid-run. A task
        that already finished returns at once; a failed one raises RestoreTaskError."""
        self._poll_task(
            self.node(),
            upid,
            on_progress,
            poll_interval=poll_interval,
            now=now,
            sleep=sleep,
            should_cancel=should_cancel,
        )

    def _poll_task(
        self,
        node: str,
        upid: str,
        on_progress: Callable[[dict[str, Any]], None] | None,
        *,
        poll_interval: float,
        now: Callable[[], float],
        sleep: Callable[[float], None],
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        start = now()
        last_progress_at = start
        last_percent = 0
        seen_log_lines = 0
        recent_lines: list[str] = []  # tail of the task log, for the failure message
        factor = float(self.config.get("stall_backstop_factor") or 6.0)
        while True:
            if should_cancel and should_cancel():
                try:
                    self.stop_task(upid, node)  # abort the qmrestore on the host
                except Exception as exc:
                    logger.debug("stop_task on cancel (ignored): %s", exc)
                raise RestoreCancelled("restore cancelled by operator")
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
                text = entry.get("t", "") if isinstance(entry, dict) else str(entry)
                if text:
                    recent_lines.append(text)
                prog = parse_task_progress(text)
                if prog:
                    last_progress_at = now()
                    last_percent = prog.get("percent", last_percent)
                    if on_progress:
                        on_progress(prog)
            seen_log_lines += len(log)
            del recent_lines[:-40]  # keep only the tail

            if status.get("status") == "stopped":
                if status.get("exitstatus") not in (None, "OK"):
                    tail = "\n".join(line for line in recent_lines[-10:] if line.strip())
                    raise RestoreTaskError(classify_task_error(str(status.get("exitstatus")), tail))
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
        """Prepare the restored guest's config for an isolated sandbox boot, in one
        config update:
        - re-point every NIC to the sandbox bridge + this env's VLAN tag (keeping
          MACs, spec §10);
        - eject any CD-ROM still backed by a source-cluster ISO (whose storage
          doesn't exist here, which would fail the start);
        - drop every ``hostpciN`` PCI passthrough: the physical device doesn't
          exist (or isn't free) on the scratch host, so the VM couldn't start with
          it. The restore itself set it (we restore as root), so it's removed here
          before the start."""
        node = self.node()
        config = self.pve.get(f"/nodes/{node}/qemu/{vmid}/config") or {}
        bridge = self.config["sandbox_bridge"]
        changes = {}
        for key, value in config.items():
            if re.fullmatch(r"net\d+", key) and isinstance(value, str):
                changes[key] = rewrite_net_line(value, bridge, vlan_tag)
        changes.update(eject_cdroms(config))
        if changes:
            self.pve.post(f"/nodes/{node}/qemu/{vmid}/config", changes)
        # Deleting hostpci is root-only (like setting it), so it goes over SSH —
        # separately from the token-safe NIC/CD-ROM update above.
        hostpci = sorted(k for k in config if re.fullmatch(r"hostpci\d+", k))
        if hostpci:
            self.pve_root.post(f"/nodes/{node}/qemu/{vmid}/config", {"delete": ",".join(hostpci)})

    def start(self, vmid: int) -> None:
        self.pve.post(f"/nodes/{self.node()}/qemu/{vmid}/status/start", {})

    def wait_unlocked(self, vmid: int, *, attempts: int = 120, sleep: Callable[[float], None] = time.sleep) -> None:
        """Wait until the VM has no config ``lock``. A still-running restore holds
        a lock, and starting a locked VM fails with "can't lock file" — which is
        what happens if the restore task is still in progress (e.g. an app restart
        resumed the tail, or a stale/duplicate task). Bounded; raises if it never
        clears."""
        node = self.node()
        lock = None
        for _ in range(attempts):
            cfg = self.pve.get(f"/nodes/{node}/qemu/{vmid}/config") or {}
            lock = cfg.get("lock")
            if not lock:
                return
            sleep(1.0)
        raise RestoreTaskError(f"VM {vmid} still locked ('{lock}') — restore not finished, not starting")

    def guest_agent_ips(self, vmid: int) -> list[str]:
        """Non-loopback IPv4 addresses the QEMU guest agent reports for the VM, or
        [] if the agent isn't up yet or absent. Used to discover the IP of a VM
        with no recorded/static-known address (the agent reports it after boot)."""
        import ipaddress

        try:
            res = self.pve.get(f"/nodes/{self.node()}/qemu/{vmid}/agent/network-get-interfaces")
        except Exception as exc:
            logger.debug("guest agent not ready for %s: %s", vmid, exc)
            return []
        ifaces = res.get("result") if isinstance(res, dict) else res
        ips: list[str] = []
        for iface in ifaces or []:
            for a in iface.get("ip-addresses") or []:
                ip = a.get("ip-address", "")
                if a.get("ip-address-type") != "ipv4" or not ip:
                    continue
                try:
                    addr = ipaddress.ip_address(ip)
                except ValueError:
                    continue
                if not addr.is_loopback and not addr.is_link_local:
                    ips.append(ip)
        return ips

    def shutdown(self, vmid: int, *, force_after: bool = True) -> None:
        node = self.node()
        try:
            self.pve.post(f"/nodes/{node}/qemu/{vmid}/status/shutdown", {"timeout": 60})
        except Exception:
            if force_after:
                self.pve.post(f"/nodes/{node}/qemu/{vmid}/status/stop", {})

    def _is_running(self, vmid: int, *, is_lxc: bool) -> bool:
        kind = "lxc" if is_lxc else "qemu"
        try:
            st = self.pve.get(f"/nodes/{self.node()}/{kind}/{vmid}/status/current")
        except Exception:
            return False
        return isinstance(st, dict) and st.get("status") == "running"

    def destroy(self, vmid: int, *, is_lxc: bool = False, sleep: Callable[[float], None] = time.sleep) -> None:
        """Destroy a guest. PVE refuses to destroy a *running* one, so ALWAYS stop
        first and wait until it is actually down before purging. The stop is not
        gated on a status read: right after a start the status can still read
        non-running, which previously skipped the stop and made the delete fail."""
        kind = "lxc" if is_lxc else "qemu"
        node = self.node()
        try:
            self.pve.post(f"/nodes/{node}/{kind}/{vmid}/status/stop", {})
        except Exception as exc:
            # Already stopped / mid-transition: harmless, we confirm below.
            logger.debug("stop before destroy (%s %s): %s", kind, vmid, exc)
        for _ in range(30):
            if not self._is_running(vmid, is_lxc=is_lxc):
                break
            sleep(1.0)
        self.pve.delete(f"/nodes/{node}/{kind}/{vmid}?purge=1&destroy-unreferenced-disks=1")

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

    def ensure_helper_template(self) -> str:
        """The helper CT template on the restore host, as a volid — uploaded via
        the API if not already there. The image is built into the app (Dockerfile)
        and uploaded on demand, so there is nothing for the operator to prepare or
        upload, and no SSH. The uploaded name carries a content hash, so a changed
        image (e.g. a fixed agent) uploads as a new template instead of reusing a
        stale one."""
        import hashlib

        node = self.node()
        data = self._helper_image_bytes()
        name = f"restore-helper-{hashlib.sha256(data).hexdigest()[:8]}.tar.gz"
        stores = [s for s in (self.pve.get(f"/nodes/{node}/storage") or []) if "vztmpl" in (s.get("content") or "")]
        if not stores:
            raise RestoreTaskError("no vztmpl-capable storage on the restore host to hold the helper template")
        current = None
        for s in stores:
            for vol in self.pve.get(f"/nodes/{node}/storage/{s['storage']}/content?content=vztmpl") or []:
                if str(vol.get("volid", "")).rsplit("/", 1)[-1] == name:
                    current = vol["volid"]
        if current is None:
            storage = stores[0]["storage"]
            upid = self.pve.upload(node, storage, name, data)
            if isinstance(upid, str) and upid.startswith("UPID"):
                self._wait_task(node, upid)
            current = f"{storage}:vztmpl/{name}"
        # Every agent change uploads a new content-hashed template; prune the old
        # ones so they don't pile up on the restore host (keep only the current).
        self._prune_helper_templates(node, stores, keep=name)
        return current

    def _prune_helper_templates(self, node: str, stores: list[dict[str, Any]], keep: str) -> None:
        """Delete stale helper CT templates (previous content hashes) so they don't
        accumulate on the restore host — keep only ``keep`` (the current one).
        Best-effort, and only ever touches our own ``restore-helper-<hash>.tar.gz``
        volumes, never other templates the operator keeps."""
        ours = re.compile(r"^restore-helper-[0-9a-f]{8}\.tar\.gz$")
        for s in stores:
            for vol in self.pve.get(f"/nodes/{node}/storage/{s['storage']}/content?content=vztmpl") or []:
                volid = str(vol.get("volid", ""))
                base = volid.rsplit("/", 1)[-1]
                if base == keep or not ours.match(base):
                    continue
                try:
                    self.pve.delete(f"/nodes/{node}/storage/{s['storage']}/content/{volid}")
                    logger.info("pruned stale helper template %s", volid)
                except Exception as exc:
                    logger.warning("could not delete stale helper template %s: %s", volid, exc)

    def _helper_image_bytes(self) -> bytes:
        from pathlib import Path

        from app.config import BASE_DIR

        path = Path(self.config.get("helper_image_path") or (Path(BASE_DIR) / "assets" / HELPER_IMAGE_NAME))
        try:
            return path.read_bytes()
        except OSError as exc:
            raise RestoreTaskError(f"helper image not found at {path}: {exc}") from exc

    def _wait_task(
        self, node: str, upid: str, *, attempts: int = 120, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        for _ in range(attempts):
            st = self.pve.get(f"/nodes/{node}/tasks/{upid}/status") or {}
            if st.get("status") == "stopped":
                if st.get("exitstatus") not in (None, "OK"):
                    raise RestoreTaskError(f"helper template upload failed: {st.get('exitstatus')}")
                return
            sleep(1.0)

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
        )
        # All via the API token: a non-root token may pass an ostemplate that is a
        # storage VOLUME ID, just not an absolute filesystem path ("Only root can
        # pass arbitrary filesystem paths"). So resolve the template to its volid
        # first; rootfs goes on the selected container-capable target storage.
        template = self.ensure_helper_template()
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
        # Configure the helper over its management IP — no SSH. The baked-in agent
        # (restore_helper_agent) serves DHCP and answers probes; we hand it the
        # dnsmasq config and the sandbox alias addresses once it is reachable.
        try:
            self._agent_wait_healthy(mgmt_ip)
        except RestoreTaskError as exc:
            # The agent never answered on the management IP. Capture the helper's
            # actual network state from inside (host SSH, failure path only) so the
            # run error names the cause — IP not applied inside vs L2/VLAN/ARP —
            # instead of a bare "not reachable".
            raise RestoreTaskError(f"{exc} | {self._helper_netdiag(helper_vmid, cfg.get('mgmt_gateway', ''))}") from exc
        conf = dnsmasq_config(reservations)
        res = self._agent_post(mgmt_ip, "/dnsmasq", {"conf": conf, "aliases": alias_addrs})
        if isinstance(res, dict) and res.get("ok") is False:
            raise RestoreTaskError(f"helper dnsmasq setup failed: {res.get('dnsmasq_error', 'unknown error')}")

    def start_lxc(self, vmid: int) -> None:
        self.pve.post(f"/nodes/{self.node()}/lxc/{vmid}/status/start", {})

    def probe(self, mgmt_ip: str, targets: list[dict[str, Any]], require_icmp: bool = True) -> dict[str, Any]:
        """Ask the helper's agent (over its management IP, not SSH) to probe the
        restored guest and return its structured JSON result."""
        return self._agent_post(mgmt_ip, "/probe", {"targets": targets, "require_icmp": require_icmp})

    def _agent_url(self, mgmt_ip: str, path: str) -> str:
        from app.services.restore_helper_agent import AGENT_PORT

        return f"http://{mgmt_ip}:{AGENT_PORT}{path}"

    def _agent_post(self, mgmt_ip: str, path: str, body: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        data = json.dumps(body).encode()
        req = urllib.request.Request(
            self._agent_url(mgmt_ip, path), data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # The agent catches its own exceptions and returns {"error", "trace"}
            # with a 500. urllib turns that into a bare HTTPError whose str() is
            # just "HTTP Error 500: Internal Server Error" — so read the body and
            # surface the agent's actual reason instead of discarding it.
            detail = exc.read().decode("utf-8", errors="replace")
            raise RestoreTaskError(f"restore helper agent {path} returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RestoreTaskError(f"restore helper agent {path} unreachable at {mgmt_ip}: {exc.reason}") from exc
        return json.loads(raw) if raw else {}

    def _agent_wait_healthy(
        self,
        mgmt_ip: str,
        *,
        deadline_seconds: float = 60.0,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Poll the just-started helper agent on its management IP and return the
        instant it answers — we ask it for ready rather than waiting a fixed time.
        Hard wall-clock cap (``deadline_seconds``, default 60s): if the agent isn't
        reachable within a minute the run fails fast, so we don't sink time (or a
        costly restore) into a helper that never came up. The cap is wall-clock,
        not an attempt count, so slow per-try connect timeouts can't stretch it."""
        import urllib.request

        deadline = now() + deadline_seconds
        while now() < deadline:
            try:
                with urllib.request.urlopen(self._agent_url(mgmt_ip, "/health"), timeout=2) as resp:
                    if resp.status == 200:
                        return
            except Exception as exc:
                logger.debug("helper agent not ready yet at %s: %s", mgmt_ip, exc)
            sleep(1.0)
        raise RestoreTaskError(
            f"restore helper agent not ready at {mgmt_ip} within {int(deadline_seconds)}s "
            "(management leg not reachable?)"
        )

    def _helper_netdiag(self, vmid: int, gateway: str = "") -> str:
        """Best-effort snapshot of the helper's network *from inside the container*
        (host SSH via ``pct exec``), for diagnosing an unreachable management leg.
        Shows whether the leg got its IP (IP-not-applied vs L2/VLAN/ARP) and
        whether the helper can reach its own gateway. Never raises."""
        cmds = [
            ("addr", f"pct exec {vmid} -- ip -4 -o addr show"),
            ("route", f"pct exec {vmid} -- ip route show"),
        ]
        if gateway:
            cmds.append(("gw-ping", f"pct exec {vmid} -- ping -c1 -W2 {gateway}"))
        parts = []
        for label, cmd in cmds:
            try:
                out = self.ssh.run(cmd).strip().replace("\n", " ")
                parts.append(f"{label}: {out or 'OK'}")
            except Exception as exc:  # non-zero exit (e.g. gw unreachable) or SSH issue
                parts.append(f"{label}: ERR {str(exc)[:80]}")
        return "helper netdiag [" + " || ".join(parts) + "]"

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


class RestoreCancelled(RuntimeError):
    """Raised inside the pipeline when the operator asked to abort the run."""


def _host_of(url: str) -> str:
    m = re.search(r"https?://([^:/]+)", url or "")
    return m.group(1).lower() if m else ""


def _stable_mac(ip: str) -> str:
    """A deterministic, locally-administered unicast MAC derived from an IPv4
    address (``02:00:`` + the four address bytes). Lets a helper that reuses a
    management IP across runs keep the same MAC, so the upstream gateway's ARP
    entry for that IP stays valid instead of pointing at a destroyed helper."""
    import ipaddress

    octets = int(ipaddress.IPv4Address(ip)).to_bytes(4, "big")
    return "02:00:" + ":".join(f"{b:02x}" for b in octets)


def _prefix_from_netmask(netmask: str) -> int:
    import ipaddress

    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError):
        return 24


def dnsmasq_config(reservations: list[dict[str, Any]]) -> str:
    """Build a dnsmasq config that hands each VM NIC its angestammte IP by MAC and
    enables DHCP in *static* mode on each distinct subnet — so dnsmasq serves ONLY
    the reserved MACs, never a dynamic pool (spec §10). ``reservations`` are
    ``{mac, ip, prefix}``. The helper's own per-subnet address is added as an
    interface alias by the caller, not via a dhcp-range."""
    import ipaddress

    # interface=sbx + bind-interfaces: serve DHCP ONLY on the sandbox leg, never
    # on the management leg. Critical when the guest's subnet overlaps the
    # management subnet — otherwise dnsmasq would answer DHCP on the real
    # management network (a rogue DHCP server). SBX_IFACE in the agent is "sbx"
    # and net0's PVE name is "sbx", so the in-container interface is "sbx".
    lines = [
        "# generated by backup-sentinel restore helper",
        "interface=sbx",
        "bind-interfaces",
        "except-interface=lo",
        "no-resolv",
    ]
    subnets: dict[str, Any] = {}
    for r in reservations:
        net = ipaddress.ip_network(f"{r['ip']}/{r['prefix']}", strict=False)
        subnets[str(net)] = net
        lines.append(f"dhcp-host={r['mac']},{r['ip']},infinite")
    for net in subnets.values():
        # Static mode ("<subnet>,static,<netmask>,<lease>"): DHCP is enabled on the
        # subnet but only the dhcp-host reservations above get a lease.
        lines.append(f"dhcp-range={net.network_address},static,{net.netmask},infinite")
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


def helper_legs(
    bridge: str,
    vlan_tag: int,
    mgmt_iface: str,
    mgmt_ip: str,
    mgmt_prefix: int,
    mgmt_vlan: int | None,
    mgmt_gateway: str,
    sandbox_aliases: list[dict[str, Any]],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Decide the helper's network legs. Returns (net_ifaces, extra_alias_addrs).

    Always dual-homed: net0 on the sandbox VLAN carries an address in the guest's
    subnet (so the helper can serve DHCP and probe the guest on its isolated L2),
    and net1 on the management interface carries the management IP + gateway (so
    the app, which runs elsewhere, can reach the helper's agent). Extra sandbox
    subnets are aliased onto net0.

    The two legs stay separate even when the guest's IP falls numerically in the
    management subnet: the guest lives on the *isolated* sandbox bridge, not on
    the management L2, so no single leg can reach both the guest and the app.
    (A previous "single-homed" optimization collapsed onto the sandbox leg in
    that case, which stranded the management IP on a portless bridge — the agent
    then ran but was unreachable from the app. When the subnets do overlap, the
    agent pins a per-guest host route onto the sandbox leg so probes still go out
    the right interface despite the duplicate connected route.)"""
    # Stable, IP-derived MAC on the management leg: a run reusing a management IP
    # across helper re-creations then keeps the same MAC, so the upstream
    # gateway's ARP cache (IP→MAC) stays valid. Without it each helper gets a
    # fresh random MAC, and the gateway black-holes the new helper's IP until its
    # stale ARP entry expires — the management leg is then intermittently
    # unreachable (reachable on a cold cache, dead right after a prior run).
    mgmt_net = f"name=mgmt,bridge={mgmt_iface},hwaddr={_stable_mac(mgmt_ip)},ip={mgmt_ip}/{mgmt_prefix}"
    if mgmt_gateway:
        mgmt_net += f",gw={mgmt_gateway}"
    if mgmt_vlan:
        mgmt_net += f",tag={mgmt_vlan}"
    first = sandbox_aliases[0] if sandbox_aliases else {"ip": "0.0.0.0", "prefix": 24}
    net0 = f"name=sbx,bridge={bridge},tag={vlan_tag},ip={first['ip']}/{first['prefix']}"
    return {"net0": net0, "net1": mgmt_net}, list(sandbox_aliases[1:])


# --- real transports (paramiko imported lazily) ------------------------------


class PveSsh:
    """PVE transport over root SSH using ``pvesh`` — mirrors the REST API 1:1, so
    RestoreServer's orchestration (and all its FakePve tests) are unchanged. Used
    instead of an API token because a token can NEVER set raw PCI passthrough
    (PVE checks the user is literally root@pam); as root over SSH there is no such
    restriction, and the restore of a VM with ``hostpciN`` just works."""

    def __init__(self, ssh: SshClient) -> None:
        self.ssh = ssh

    def _call(self, verb: str, path: str, params: dict[str, Any] | None = None) -> Any:
        clean_path, query = split_path_query(path)
        merged = {**query, **(params or {})}
        return parse_pvesh_output(self.ssh.run(pvesh_command(verb, clean_path, merged)))

    def get(self, path: str) -> Any:
        return self._call("get", path)

    def post(self, path: str, data: dict[str, Any]) -> Any:
        # Config updates are PUT (pvesh "set"); everything else (restore, create,
        # status/start) is POST (pvesh "create"). pvesh create on /qemu with an
        # archive IS a root qmrestore, which can set hostpci.
        verb = "set" if split_path_query(path)[0].rstrip("/").endswith("/config") else "create"
        return self._call(verb, path, data)

    def delete(self, path: str) -> Any:
        return self._call("delete", path)

    def upload(self, node: str, storage: str, filename: str, data: bytes) -> Any:
        """Place the helper CT template by writing it straight into the target
        storage's ``template/cache`` directory over SFTP (no API multipart). The
        write is synchronous, so there is no task to wait on — return None."""
        info = self._call("get", f"/storage/{storage}") or {}
        base = info.get("path") if isinstance(info, dict) else None
        if not base:
            raise RestoreTaskError(f"storage {storage} has no filesystem path for the template upload")
        cache = f"{base.rstrip('/')}/template/cache"
        self.ssh.run(f"mkdir -p {shlex.quote(cache)}")
        self.ssh.put_bytes(f"{cache}/{filename}", data)
        return None


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

    def upload(self, node: str, storage: str, filename: str, data: bytes) -> Any:
        from app.services.api_client import api_upload

        return api_upload(
            self.base,
            f"/nodes/{node}/storage/{storage}/upload",
            self.auth,
            content="vztmpl",
            filename=filename,
            data=data,
            label="PVE-Restore",
        )


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

    def put_bytes(self, remote_path: str, data: bytes) -> None:
        """Write ``data`` to ``remote_path`` over SFTP (used to place the helper
        CT template into a storage's template/cache directory)."""
        if self._client is None:
            self._client = self._connect()
        sftp = self._client.open_sftp()
        try:
            with sftp.open(remote_path, "wb") as fh:
                fh.write(data)
        finally:
            sftp.close()
