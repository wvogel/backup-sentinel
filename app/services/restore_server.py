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
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlsplit

from app.services.netdiag_format import format_netdiag

logger = logging.getLogger(__name__)

OWNED_TAG = "bsentinel-restore"
HELPER_IMAGE_NAME = "restore-helper.tar.gz"  # the app-built helper CT template

# Nominal sustained throughput per disk class (bytes/s), the denominator for the
# Disk-I/O ring's read/write arcs. These are rough tier figures, not measured —
# the ring's arcs are "share of what this tier can do", and the %util in the
# centre is the measured truth. Tunable later from the settings UI.
DISK_CLASS_MAX_BPS = {
    "enterprise-ssd": 2000 * 1_000_000,
    "standard-ssd": 550 * 1_000_000,
    "sas-hdd": 250 * 1_000_000,
    "sata-hdd": 180 * 1_000_000,
}
_DISK_CLASS_FALLBACK_BPS = DISK_CLASS_MAX_BPS["standard-ssd"]

# Whole physical block devices only — never partitions (…p1 / sda1), dm-*, loop,
# md or zd, so a device and its slices aren't double-counted.
_PHYS_DISK_RE = re.compile(r"^(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|xvd[a-z]+)$")
_SECTOR_BYTES = 512

# Storage types whose I/O goes over the network, not through the host's own
# disks — /proc/diskstats never sees it, so they don't set the disk ring's scale.
_NETWORK_STORAGE_TYPES = frozenset({"nfs", "cifs", "glusterfs", "cephfs", "rbd", "iscsi", "iscsidirect", "pbs", "esxi"})

# Last host counter sample per node, so the next refresh can turn the monotonic
# /proc counters into rates. Keyed by node name; guarded because refreshes may
# overlap. Written only from free_resources().
_IO_SAMPLE_CACHE: dict[str, dict[str, Any]] = {}
_IO_SAMPLE_LOCK = threading.Lock()


def _parse_meminfo(text: str) -> dict[str, int] | None:
    """/proc/meminfo → bytes dict with total/free/buff_cache/available/used, or
    None if MemTotal is absent."""
    kb: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.strip().split()
        if parts and parts[0].isdigit():
            kb[key.strip()] = int(parts[0]) * 1024  # meminfo is in kB
    if "MemTotal" not in kb:
        return None
    total = kb["MemTotal"]
    free = kb.get("MemFree", 0)
    buff_cache = kb.get("Buffers", 0) + kb.get("Cached", 0) + kb.get("SReclaimable", 0)
    return {
        "total": total,
        "free": free,
        "buff_cache": buff_cache,
        "available": kb.get("MemAvailable", free + buff_cache),
        "used": max(0, total - free - buff_cache),
    }


def _parse_diskstats(text: str) -> dict[str, tuple[int, int, int]]:
    """/proc/diskstats → {device: (bytes_read, bytes_written, busy_ms)} for whole
    physical devices only. Fields (1-based): 3=name, 6=sectors read, 10=sectors
    written, 13=io_ticks (ms the device was busy)."""
    out: dict[str, tuple[int, int, int]] = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) < 14 or not _PHYS_DISK_RE.match(f[2]):
            continue
        try:
            out[f[2]] = (int(f[5]) * _SECTOR_BYTES, int(f[9]) * _SECTOR_BYTES, int(f[12]))
        except ValueError:
            continue
    return out


def _parse_netdev(text: str) -> dict[str, tuple[int, int]]:
    """/proc/net/dev → {iface: (rx_bytes, tx_bytes)}. Skips the two header lines
    and loopback. rx is the 1st column after the colon, tx the 9th."""
    out: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        name, sep, rest = line.partition(":")
        name = name.strip()
        if not sep or name == "lo":
            continue
        cols = rest.split()
        if len(cols) < 16:
            continue
        try:
            out[name] = (int(cols[0]), int(cols[8]))
        except ValueError:
            continue
    return out


def _parse_speeds(text: str) -> dict[str, int]:
    """ "<iface> <speed-mbit>" lines → {iface: mbit}. A blank/invalid/negative
    speed (a down link reports -1 or nothing) is dropped, so only uplinks with a
    negotiated speed remain. The caller already restricts the lines to physical
    NICs, so virtual stacks (bonds/bridges/VLANs/veth) never appear here."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                mbit = int(parts[1])
            except ValueError:
                continue
            if mbit > 0:
                out[parts[0]] = mbit
    return out


def compute_io_rates(
    prev: dict[str, Any] | None,
    cur: dict[str, Any],
    disk_max_bps: int,
) -> dict[str, Any]:
    """Turn two counter samples into rates. ``cur``/``prev`` carry ``t`` (host
    uptime seconds — monotonic, immune to SSH latency), ``disk`` and ``net``.
    Returns zeros with ``have=False`` when there is no usable prior sample or a
    counter went backwards (reboot / wrap)."""
    zero = {
        "have": False,
        "disk_read": 0,
        "disk_write": 0,
        "disk_util": 0,
        "disk_read_pct": 0,
        "disk_write_pct": 0,
        "net_in": 0,
        "net_out": 0,
        "net_in_pct": 0,
        "net_out_pct": 0,
        "net_link": cur.get("link_bps", 0),
    }
    if not prev:
        return zero
    dt = cur["t"] - prev["t"]
    if dt <= 0:
        return zero  # reboot or clock went backwards — reset, no rate this round

    disk_read = disk_write = 0
    util_pct = 0.0
    for dev, (r, w, busy) in cur["disk"].items():
        if dev not in prev["disk"]:
            continue
        pr, pw, pbusy = prev["disk"][dev]
        if r < pr or w < pw or busy < pbusy:
            return zero  # counter reset — don't emit a bogus spike
        disk_read += (r - pr) / dt
        disk_write += (w - pw) / dt
        util_pct = max(util_pct, (busy - pbusy) / (dt * 1000.0) * 100.0)

    net_in = net_out = 0
    for iface, (rx, tx) in cur["net"].items():
        if iface not in prev["net"]:
            continue
        prx, ptx = prev["net"][iface]
        if rx < prx or tx < ptx:
            return zero
        net_in += (rx - prx) / dt
        net_out += (tx - ptx) / dt

    link = cur.get("link_bps", 0)
    pct = lambda v, cap: min(100, round(v / cap * 100)) if cap else 0  # noqa: E731
    return {
        "have": True,
        "disk_read": round(disk_read),
        "disk_write": round(disk_write),
        "disk_util": min(100, round(util_pct)),
        "disk_read_pct": pct(disk_read, disk_max_bps),
        "disk_write_pct": pct(disk_write, disk_max_bps),
        "net_in": round(net_in),
        "net_out": round(net_out),
        "net_in_pct": pct(net_in, link),
        "net_out_pct": pct(net_out, link),
        "net_link": link,
    }


class PveClient(Protocol):
    def get(self, path: str) -> Any: ...
    def post(self, path: str, data: dict[str, Any]) -> Any: ...
    def delete(self, path: str) -> Any: ...
    def upload(self, node: str, storage: str, filename: str, data: bytes) -> Any: ...


class SshClient(Protocol):
    def run(self, command: str) -> str: ...
    def put_bytes(self, remote_path: str, data: bytes) -> None: ...


# --- pure helpers ------------------------------------------------------------


def rewrite_net_line(net_line: str, bridge: str, tag: int | None, trunks: int | None = None) -> str:
    """Rewrite a PVE ``netN`` config value to a new bridge (+ optional VLAN tag)
    while **preserving the MAC address and model** and dropping any old tag/trunks.

    ``virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0,tag=5`` → ``...,bridge=vmbr9,tag=901``

    ``trunks`` carries an extra tagged VLAN through the port (for a guest that tags
    internally, e.g. its IP is on ``eth0.20``): the env's ``tag`` stays the PVID
    (per-env isolation for untagged traffic) and ``trunks=20`` lets the guest's
    tagged frames pass. ``...,tag=901,trunks=20``.

    Also drop ``firewall=1``: with the Proxmox NIC firewall enabled the guest hangs
    off a firewall bridge (fwbr/fwpr) whose rules silently drop the probe's ICMP
    and TCP (ARP still resolves, so it looks "reachable but dead"). The sandbox is
    already isolated by the portless bridge, so the per-NIC firewall only gets in
    the way — omitting it leaves PVE's default (off).

    And pin the MTU to 1500: a source VM on a jumbo-frame network keeps e.g.
    ``mtu=9000``, and PVE refuses to start it on the sandbox bridge (MTU 1500 —
    "MTU '9000' is bigger than the bridge MTU"). PVE honours ``mtu`` for VirtIO
    only; other models get none and so use the bridge's 1500 anyway."""
    parts = [p for p in net_line.split(",") if p]
    kept = [p for p in parts if not p.startswith(("bridge=", "tag=", "trunks=", "firewall=", "mtu="))]
    kept.append(f"bridge={bridge}")
    if parts and parts[0].startswith("virtio"):
        kept.append("mtu=1500")
    if tag is not None:
        kept.append(f"tag={tag}")
    if trunks is not None:
        kept.append(f"trunks={trunks}")
    return ",".join(kept)


def sandbox_name(cluster: str, vm: str, suffix: str = "") -> str:
    """A hostname-safe name for a sandbox guest or helper, built from the cluster
    and VM names — so an operator can tell at a glance what the reused vmids 9000
    (restore VM) and 9001 (helper) currently belong to, instead of a bare number.
    Lower-cased, every run of non ``[a-z0-9-]`` collapsed to a single ``-``,
    trimmed, and kept within the 63-char hostname limit (``suffix`` — e.g.
    ``helper`` — is always preserved)."""
    base = "-".join(p for p in (cluster or "", vm or "") if p)
    base = re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-") or "restore"
    if suffix:
        base = base[: max(1, 63 - len(suffix) - 1)].strip("-")
        return f"{base}-{suffix}" if base else suffix
    return base[:63].strip("-") or "restore"


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

    def _host_sample(self) -> dict[str, Any] | None:
        """One root-SSH round-trip for the whole host snapshot: memory, disk and
        network counters, NIC link speeds and the host's uptime clock. Reading all
        of it in a single ``cat`` keeps the host gentle — one exec per ~10 s host
        refresh, never a per-request or sub-second poll. Returns parsed sections,
        or None if the read fails (caller falls back to PVE's node status)."""
        cmd = (
            "cat /proc/meminfo; echo '@@DISK'; cat /proc/diskstats; "
            "echo '@@NET'; cat /proc/net/dev; echo '@@SPEED'; "
            # Only physical NICs (those with a /device link) report a speed here.
            # Bonds, bridges, VLANs and veth echo the underlying link's speed too,
            # and summing them all multiply-counts one physical link (a single
            # 10GbE uplink came out as ~50 Gbit/s across nic + bridge + veths).
            'for d in /sys/class/net/*; do [ -e "$d/device" ] && echo "$(basename "$d") $(cat "$d/speed" 2>/dev/null)"; done; '
            "echo '@@UP'; cat /proc/uptime"
        )
        try:
            out = self.ssh.run(cmd)
        except Exception as exc:
            logger.debug("host sample read failed: %s", exc)
            return None
        sections: dict[str, list[str]] = {"MEM": []}
        cur = "MEM"
        for line in out.splitlines():
            if line.startswith("@@"):
                cur = line[2:]
                sections[cur] = []
            else:
                sections.setdefault(cur, []).append(line)
        mem = _parse_meminfo("\n".join(sections.get("MEM", [])))
        if mem is None:
            return None
        up_parts = " ".join(sections.get("UP", [])).split()
        try:
            uptime = float(up_parts[0]) if up_parts else time.monotonic()
        except ValueError:
            uptime = time.monotonic()
        speeds = _parse_speeds("\n".join(sections.get("SPEED", [])))
        net = _parse_netdev("\n".join(sections.get("NET", [])))
        # Only physical uplinks with a negotiated speed count toward the Network
        # ring (the sample emits a speed for physical NICs only), so bonds,
        # bridges, VLANs and veth never inflate the link denominator or the
        # in/out counters by echoing the underlying link's speed and traffic.
        net_uplinks = {i: v for i, v in net.items() if i in speeds}
        return {
            "t": uptime,
            "mem": mem,
            "disk": _parse_diskstats("\n".join(sections.get("DISK", []))),
            "net": net_uplinks or net,  # fall back to all ifaces if none has a speed
            "link_bps": sum(speeds[i] for i in net_uplinks) * 1_000_000 // 8,
        }

    def _disk_ceiling_bps(self, classes: dict[str, str | None], selected: set[str]) -> int:
        """Denominator for the disk read/write arcs: the fastest tier among the
        selected *local* target storages (the ring counts local disks only, so a
        network storage's tier must not set its scale). Falls back to standard-ssd."""
        vals = [DISK_CLASS_MAX_BPS[c] for s in selected if (c := classes.get(s)) in DISK_CLASS_MAX_BPS]
        return max(vals) if vals else _DISK_CLASS_FALLBACK_BPS

    def free_resources(self) -> dict[str, Any]:
        node = self.node()
        status = self.pve.get(f"/nodes/{node}/status") or {}
        mem = status.get("memory", {})
        # Prefer the host's real figures (incl. MemAvailable) over PVE's node
        # status, whose "free" treats reclaimable buff/cache as used. One SSH read
        # also carries the disk/net counters for the I/O rings.
        sample = self._host_sample()
        mem_detail = sample["mem"] if sample else None
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
        if mem_detail:
            # ram_free = AVAILABLE (what a restore can really use); the ring and the
            # RAM-fit check both read ram_free, so both become honest.
            ram_total, ram_free = mem_detail["total"], mem_detail["available"]
        else:
            ram_total, ram_free = mem.get("total"), mem.get("free")
        local = {s["storage"] for s in stores if s["type"] not in _NETWORK_STORAGE_TYPES}
        io = self._io_rates(node, sample, self._disk_ceiling_bps(classes, local))
        return {"ram_free": ram_free, "ram_total": ram_total, "ram_detail": mem_detail, "stores": stores, "io": io}

    def _io_rates(self, node: str, sample: dict[str, Any] | None, disk_max_bps: int) -> dict[str, Any]:
        """Disk/net rates from this sample vs. the previous one for this node.
        Updates the per-node counter cache. The first refresh has no prior sample,
        so it reports have=False (the rings show '—' until the next tick)."""
        if sample is None:
            return compute_io_rates(None, {"disk": {}, "net": {}, "t": 0, "link_bps": 0}, disk_max_bps)
        with _IO_SAMPLE_LOCK:
            prev = _IO_SAMPLE_CACHE.get(node)
            _IO_SAMPLE_CACHE[node] = {"t": sample["t"], "disk": sample["disk"], "net": sample["net"]}
        return compute_io_rates(prev, sample, disk_max_bps)

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
        """Ensure the VLAN-aware, portless (isolated) sandbox bridge actually EXISTS
        and is UP on the host (spec §6).

        Create it the **PVE-native** way (``pvesh create /nodes/<node>/network``,
        config only) so it is written to the host's network config — persistent
        across reboots AND visible in the PVE GUI (Node → Network). The old
        approach only appended an ``interfaces.d`` stanza and ran ``ifup`` when the
        config was absent, so a single failed ``ifup`` (or a reboot where
        ``interfaces.d`` is not sourced) left the bridge missing/down and never
        retried — the restore then ran on a bridge that did not exist, and it
        never showed in the GUI.

        We deliberately do **not** apply the config host-wide here (no
        ``pvesh set /nodes/<node>/network``): that runs ``ifreload -a``, which
        bounces *every* interface on the restore host — including vmbr0's
        production VLAN sub-interfaces (e.g. the management VLAN the helper's mgmt
        leg lives on). A single such reload could leave those sub-interfaces stale
        ("No route to host" to the helper, PBS "client error (Connect)") until the
        host is rebooted. The runtime ``ip link`` pass below makes vmbr9 live right
        now without touching anything else; the operator can apply the pending GUI
        change (or a reboot picks it up) at a calm moment. A final check aborts the
        run with a clear error if the bridge is still missing."""
        bridge = self.config["sandbox_bridge"]
        node = self.node()
        b = shlex.quote(bridge)
        qnode = shlex.quote(node)
        # 1. Native, persistent, GUI-visible bridge (config only — NO host-wide
        #    apply; see the docstring: an ``ifreload -a`` would bounce the host's
        #    production VLANs). Skip if the node already has it configured.
        #    Best-effort: the runtime pass below makes it work regardless.
        already = False
        try:
            already = bool(self.pve.get(f"/nodes/{node}/network/{bridge}"))
        except Exception:
            already = False
        if not already:
            try:
                self.ssh.run(
                    f"pvesh create /nodes/{qnode}/network --iface {b} --type bridge "
                    f"--autostart 1 --bridge_vlan_aware 1 --bridge_vids 2-4094"
                )
            except Exception as exc:
                logger.warning("PVE-native creation of %s failed (will bring it up at runtime): %s", bridge, exc)
        # 2. Guarantee it is up NOW (idempotent) regardless of the apply's timing.
        runtime = (
            "set -e\n"
            f"if ! ip link show {b} >/dev/null 2>&1; then ip link add name {b} type bridge; fi\n"
            f"ip link set {b} type bridge vlan_filtering 1\n"
            f"ip link set {b} up\n"
            f"ip link show {b} >/dev/null 2>&1\n"
        )
        try:
            self.ssh.run(runtime)
        except Exception as exc:
            raise RestoreTaskError(
                f"could not bring up the isolated sandbox bridge {bridge}: {str(exc)[:200]}"
            ) from exc

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
        restore).

        The guest is NOT named here. ``qmrestore`` holds a flock on the config for
        the restore's whole duration (``qm set --skiplock`` only skips the config
        *property* lock, not the file lock, so it just times out), and ``qmrestore``
        has no ``--name``/ignores a name override — so a nameless VM shows as PVE's
        default "VM {vmid}" until the restore finishes. ``rewrite_nics`` sets the
        ``<cluster>-<vm>`` name right after, once the lock clears."""
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
        # No name override here: PVE ignores it on an archive restore, and the VM
        # is renamed by rewrite_nics once the restore lock clears (see restore()).
        self.ssh.run(
            f"nohup pvesh create /nodes/{shlex.quote(node)}/qemu "
            f"-vmid {vmid} -archive {shlex.quote(archive)} -storage {shlex.quote(storage)} -live-restore 0 "
            f"> {shlex.quote(out)} 2>&1 & echo kicked"
        )
        deadline = now() + deadline_seconds
        while now() < deadline:
            # Only a RUNNING restore task for this vmid — not a finished one from a
            # previous restore of the same (reused) vmid, which would make us poll
            # an already-stopped task and jump straight to "booting/100%".
            running = [
                tk
                for tk in self.pve.get(f"/nodes/{node}/tasks?vmid={vmid}&source=active&limit=50") or []
                if str(tk.get("id")) == str(vmid)
                and "restore" in str(tk.get("type") or "").lower()
                and not tk.get("endtime")
                and tk.get("upid")
            ]
            if running:
                upid = max(running, key=lambda t: t.get("starttime", 0))["upid"]
                logger.info("restore task for VM %s: %s", vmid, upid)
                return upid
            sleep(2.0)
        tail = ""
        try:
            tail = self.ssh.run(f"tail -c 400 {shlex.quote(out)} 2>/dev/null || true").strip()
        except Exception as exc:
            logger.debug("restore log tail failed: %s", exc)
        raise RestoreTaskError(f"restore task for VM {vmid} did not appear; log: {tail[:300]}")

    def stop_task(
        self, upid: str, node: str | None = None, *, wait: bool = True, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        """Ask PVE to stop a running task (used to abort an in-flight restore on
        cancel), then wait until it is actually stopped. Waiting matters: an
        aborted ``qmrestore`` keeps the target VM's config ``lock`` until the task
        finishes unwinding, and tearing down before then fails with "VM is locked
        (create)". Best-effort; the caller tears the environment down afterwards
        (``destroy`` also clears a leftover lock, so a timeout here is not fatal)."""
        node = node or self.node()
        self.pve.delete(f"/nodes/{node}/tasks/{upid}")
        if not wait:
            return
        for _ in range(30):
            try:
                status = self.pve.get(f"/nodes/{node}/tasks/{upid}/status") or {}
            except Exception as exc:
                logger.debug("stop_task status poll (ignored): %s", exc)
                return
            if status.get("status") == "stopped":
                return
            sleep(1.0)

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
                    self.stop_task(upid, node, sleep=sleep)  # abort the qmrestore + wait for it to stop
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

    def present_vmids(self) -> set[int]:
        """Every guest vmid (qemu + lxc) on the host. Raises if the host is
        unreachable (so a caller can tell 'host down' from 'nothing there' and not
        mistake a transient outage for an empty host)."""
        node = self.node()
        out: set[int] = set()
        for kind in ("qemu", "lxc"):
            for g in self.pve.get(f"/nodes/{node}/{kind}") or []:
                try:
                    out.add(int(g.get("vmid")))
                except (TypeError, ValueError):
                    continue
        return out

    def task_running(self, upid: str, node: str | None = None) -> bool:
        """Whether a PVE task (by UPID) is still running. Unreachable/unknown → False."""
        try:
            st = self.pve.get(f"/nodes/{node or self.node()}/tasks/{upid}/status") or {}
        except Exception:
            return False
        return st.get("status") == "running"

    def find_running_restore_upid(self, vmid: int, node: str | None = None) -> str | None:
        """The UPID of a RUNNING restore task for this vmid on the host, or None.
        Used by resume when the persisted UPID was lost (a deploy that hit before
        it was written) — the restore keeps running detached on the host, so we
        rediscover it and wait for it instead of barrelling into the tail."""
        node = node or self.node()
        try:
            tasks = self.pve.get(f"/nodes/{node}/tasks?vmid={vmid}&source=active&limit=50") or []
        except Exception as exc:
            logger.debug("find_running_restore_upid failed: %s", exc)
            return None
        running = [
            t
            for t in tasks
            if str(t.get("id")) == str(vmid)
            and "restore" in str(t.get("type") or "").lower()
            and not t.get("endtime")
            and t.get("upid")
        ]
        if not running:
            return None
        return max(running, key=lambda t: t.get("starttime", 0))["upid"]

    def is_running(self, vmid: int, *, is_lxc: bool = False) -> bool:
        """Public wrapper: whether the guest is currently running (used by resume
        to decide whether the restored VM still needs a boot)."""
        return self._is_running(vmid, is_lxc=is_lxc)

    def vm_locked(self, vmid: int, *, is_lxc: bool = False) -> bool:
        """Whether the guest's config carries a lock (e.g. 'create' while a restore
        is in progress). Unreachable/unknown → False. Resume uses this as a last
        safety net so a live, locked restore is never torn down."""
        kind = "lxc" if is_lxc else "qemu"
        try:
            cfg = self.pve.get(f"/nodes/{self.node()}/{kind}/{vmid}/config") or {}
        except Exception:
            return False
        return bool(cfg.get("lock"))

    def rewrite_nics(self, vmid: int, vlan_tag: int, guest_vlan: int | None = None, name: str = "") -> None:
        """Prepare the restored guest's config for an isolated sandbox boot, in one
        config update:
        - rename it to ``name`` (cluster+VM), so it is obvious what the reused vmid
          belongs to, instead of the backup's bare original name;
        - re-point every NIC to the sandbox bridge + this env's VLAN tag (keeping
          MACs, spec §10);
        - when the guest tags internally (``guest_vlan`` set), also carry that VID
          as a trunk on the port so the guest's tagged frames reach the helper;
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
        if name:
            changes["name"] = name
        for key, value in config.items():
            if re.fullmatch(r"net\d+", key) and isinstance(value, str):
                changes[key] = rewrite_net_line(value, bridge, vlan_tag, trunks=guest_vlan)
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

    def guest_agent_nics(self, vmid: int) -> list[tuple[str, int]]:
        """Non-loopback IPv4 (address, prefix) the QEMU guest agent reports for the
        VM, or [] if the agent isn't up yet or absent. Used to discover the live IP
        of a VM that kept a static address (it never took our DHCP) — we then probe
        that IP and give the helper a host in ITS subnet. The prefix is the guest's
        real network size (not always /24).

        Only the guest's **real NICs** count: interfaces whose MAC is one of the
        VM config's ``netN`` MACs. A Docker host reports a dozen ``br-*``/``docker0``
        bridges plus ``wg0`` etc. — addresses that exist only inside the guest and
        are never on the sandbox L2; probing each one sequentially pushed a probe
        pass past the helper call's timeout. If the config has no MACs (unreadable),
        nothing is filtered."""
        import ipaddress

        from app.services.restore_ip_collect import macs_from_config

        node = self.node()
        try:
            res = self.pve.get(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces")
        except Exception as exc:
            logger.debug("guest agent not ready for %s: %s", vmid, exc)
            return []
        try:
            cfg = self.pve.get(f"/nodes/{node}/qemu/{vmid}/config")
        except Exception as exc:
            logger.debug("config unreadable for %s: %s", vmid, exc)
            cfg = None
        real_macs = set(macs_from_config(cfg).values()) if isinstance(cfg, dict) else set()
        ifaces = res.get("result") if isinstance(res, dict) else res
        nics: list[tuple[str, int]] = []
        for iface in ifaces or []:
            if real_macs and (iface.get("hardware-address") or "").lower() not in real_macs:
                continue
            for a in iface.get("ip-addresses") or []:
                ip = a.get("ip-address", "")
                if a.get("ip-address-type") != "ipv4" or not ip:
                    continue
                try:
                    addr = ipaddress.ip_address(ip)
                except ValueError:
                    continue
                if not addr.is_loopback and not addr.is_link_local:
                    prefix = a.get("prefix")
                    nics.append((ip, int(prefix) if prefix is not None else 24))
        return nics

    def guest_agent_ips(self, vmid: int) -> list[str]:
        """Just the addresses from :meth:`guest_agent_nics` (prefix dropped)."""
        return [ip for ip, _ in self.guest_agent_nics(vmid)]

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

    def unlock(self, vmid: int, *, is_lxc: bool = False) -> None:
        """Clear a guest's config ``lock`` over root SSH (``qm``/``pct unlock``).
        A cancelled/aborted ``qmrestore`` leaves the target VM locked ('create'),
        which blocks the stop and the destroy ("VM is locked (create)"). Clearing
        the lock needs root (skiplock), which we have on this dedicated host.
        Best-effort: no lock, or already gone, is not an error."""
        tool = "pct" if is_lxc else "qm"
        try:
            self.ssh.run(f"{tool} unlock {int(vmid)}")
        except Exception as exc:
            logger.debug("unlock %s %s (ignored): %s", tool, vmid, exc)

    def destroy(self, vmid: int, *, is_lxc: bool = False, sleep: Callable[[float], None] = time.sleep) -> None:
        """Destroy a guest. PVE refuses to destroy a *running* one, so ALWAYS stop
        first and wait until it is actually down before purging. The stop is not
        gated on a status read: right after a start the status can still read
        non-running, which previously skipped the stop and made the delete fail.

        First clear any config lock: a cancelled restore leaves the target VM
        locked ('create'), which would otherwise fail the stop and the delete."""
        kind = "lxc" if is_lxc else "qemu"
        node = self.node()
        self.unlock(vmid, is_lxc=is_lxc)
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
        sandbox_ip: str,
        guest_ip: str,
        sandbox_prefix: int,
        guest_vlan: int | None = None,
        label: str = "",
    ) -> None:
        """Create + start the dual-homed helper LXC: sandbox leg (this env's VLAN
        tag) running dnsmasq, management leg (static mgmt IP) for reachability.
        The helper never forwards between legs (spec §10). Its rootfs goes on the
        first selected target storage that accepts containers — no separate
        storage to configure.

        ``sandbox_ip``/``guest_ip``/``sandbox_prefix`` are decided by the
        orchestrator from the guest's known IP (recorded or operator override,
        honouring its prefix) or the management-range fallback — so the sandbox
        addressing never depends on the restored, running VM.

        When the guest tags internally (``guest_vlan`` set), the sandbox leg sits
        on that VID as an access port: PVE strips/adds the tag at the bridge, so
        the guest's tagged frames reach the helper and no in-container VLAN
        sub-interface is needed. Otherwise it stays on this env's isolated 9xx."""
        node = self.node()
        cfg = self.config
        helper_storage = self._lxc_storage()
        if not helper_storage:
            raise RuntimeError("no selected target storage accepts LXC containers (content 'rootdir')")
        bridge = cfg["sandbox_bridge"]
        mgmt_prefix = _prefix_from_netmask(cfg["mgmt_netmask"])
        netmask = _netmask_from_prefix(sandbox_prefix)
        sbx_tag = guest_vlan or vlan_tag
        net_ifaces = helper_legs(
            bridge,
            sbx_tag,
            cfg["mgmt_iface"],
            mgmt_ip,
            mgmt_prefix,
            cfg.get("mgmt_vlan"),
            cfg.get("mgmt_gateway", ""),
            sandbox_ip,
            sandbox_prefix,
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
                # Name the helper after the cluster+VM it serves (+ '-helper'), so
                # it is obvious what vmid 9001 belongs to; fall back to the vmid.
                "hostname": sandbox_name(label, "", suffix="helper") if label else f"{OWNED_TAG}-helper-{helper_vmid}",
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
        conf = dnsmasq_config(guest_ip, netmask)
        res = self._agent_post(mgmt_ip, "/dnsmasq", {"conf": conf, "aliases": []})
        if isinstance(res, dict) and res.get("ok") is False:
            raise RestoreTaskError(f"helper dnsmasq setup failed: {res.get('dnsmasq_error', 'unknown error')}")

    def start_lxc(self, vmid: int) -> None:
        self.pve.post(f"/nodes/{self.node()}/lxc/{vmid}/status/start", {})

    def probe(self, mgmt_ip: str, targets: list[dict[str, Any]], require_icmp: bool = True) -> dict[str, Any]:
        """Ask the helper's agent (over its management IP, not SSH) to probe the
        restored guest and return its structured JSON result. The agent probes the
        targets one after another (ping, ARP poke, each port with ~2-3s timeouts),
        so the HTTP timeout grows with the number of targets and ports."""
        budget = 15.0 + sum(8.0 + 3.0 * len(t.get("ports") or []) for t in targets)
        return self._agent_post(mgmt_ip, "/probe", {"targets": targets, "require_icmp": require_icmp}, timeout=budget)

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
        except TimeoutError as exc:  # a read timeout is not wrapped in URLError — bare "timed out"
            raise RestoreTaskError(f"restore helper agent {path} at {mgmt_ip} timed out after {int(timeout)}s") from exc
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

    def probe_netdiag(
        self,
        helper_vmid: int,
        guest_ips: list[str],
        *,
        restore_vmid: int | None = None,
        ports: list[int] | None = None,
        iface: str = "sbx",
    ) -> str:
        """Why a probe couldn't reach the guest, captured over host SSH (``pct
        exec`` / ``qm config`` — no helper credentials), for the run log:

        - the isolated **sandbox bridge** state (does it exist/is it up) and the
          NICs attached to it — the helper veth and the restored VM's tap must
          both be there, or they are not on the same segment;
        - the restored VM's NIC config (``bridge``/``tag``) vs the helper's — a
          VLAN mismatch means no L2 even when both are on the bridge;
        - the helper's sandbox leg address + routes;
        - per guest IP: whether ARP resolves (a MAC after a poke) and whether ICMP
          and each TCP port answer.

        ``neigh … REACHABLE`` with no ping ⇒ the guest is on the L2 but firewalls
        ICMP/ports; ``INCOMPLETE``/no entry ⇒ an L2/VLAN problem. Never raises."""
        if not helper_vmid:
            return ""
        h = int(helper_vmid)
        bridge = shlex.quote(self.config.get("sandbox_bridge", "vmbr9"))
        cmds = [
            ("bridge", f"ip -br link show {bridge}"),
            ("bridge ports", f"ls /sys/class/net/{bridge}/brif 2>/dev/null || echo none"),
            # Compressed ranges, all ports — the formatter keeps only this bridge's.
            ("bridge vlans", "bridge -c vlan show 2>/dev/null | head -300"),
        ]
        if restore_vmid:
            cmds.append(("vm nic", f"qm config {int(restore_vmid)} | grep -E '^net[0-9]' || true"))
        cmds += [
            ("sbx addr", f"pct exec {h} -- ip -4 -o addr show dev {shlex.quote(iface)}"),
            ("routes", f"pct exec {h} -- ip route show"),
        ]
        for ip in list(guest_ips)[:4]:
            q = shlex.quote(ip)
            # ping first (populates the ARP cache even if ICMP is filtered), then
            # read the neighbour table for the resolved MAC, then try the ports.
            cmds.append((f"ping {ip}", f"pct exec {h} -- ping -c1 -W2 {q}"))
            cmds.append((f"neigh {ip}", f"pct exec {h} -- ip neigh show to {q}"))
            for p in list(ports or [])[:4]:
                cmds.append((f"tcp {ip}:{int(p)}", f"pct exec {h} -- nc -z -w2 {q} {int(p)}"))
        parts: list[tuple[str, str]] = []
        for label, cmd in cmds:
            try:
                parts.append((label, self.ssh.run(cmd).strip()))
            except Exception as exc:  # non-zero exit (e.g. ping/nc failed) or SSH issue
                parts.append((label, f"ERR {str(exc)[:80]}"))
        return format_netdiag(parts)

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


def _netmask_from_prefix(prefix: int) -> str:
    import ipaddress

    try:
        return str(ipaddress.ip_network(f"0.0.0.0/{int(prefix)}").netmask)
    except (ValueError, TypeError):
        return "255.255.255.0"


def parse_cidr(text: str, default_prefix: int = 24) -> tuple[str, int] | None:
    """Parse an operator-entered ``ip`` or ``ip/prefix`` into (ip, prefix). A bare
    IP gets ``default_prefix`` (operators who set only an IP mean a /24 — existing
    values are migrated to an explicit /24). Returns None for anything unparseable
    so a bad override is ignored rather than crashing the plan."""
    import ipaddress

    raw = (text or "").strip()
    if not raw:
        return None
    ip_part, _, prefix_part = raw.partition("/")
    try:
        ip = str(ipaddress.IPv4Address(ip_part.strip()))
    except ipaddress.AddressValueError:
        return None
    if prefix_part.strip():
        try:
            prefix = int(prefix_part.strip())
        except ValueError:
            return None
        if not 0 <= prefix <= 32:
            return None
    else:
        prefix = default_prefix
    return ip, prefix


def sandbox_host_ip(guest_ip: str, prefix: int) -> str:
    """The helper's own address on the sandbox leg: any usable host in the GUEST's
    subnet that is not the guest IP itself (the lowest such host, deterministically).
    Both legs live alone on the isolated sandbox L2, so any non-guest host is free.
    Falls back to ``guest_ip + 1`` for a degenerate subnet with no other host."""
    import ipaddress

    net = ipaddress.ip_network(f"{guest_ip}/{prefix}", strict=False)
    guest = ipaddress.IPv4Address(guest_ip)
    for host in net.hosts():
        if host != guest:
            return str(host)
    return str(guest + 1)


SANDBOX_HELPER_HOST = 254  # the helper's own address on the sandbox leg
SANDBOX_GUEST_HOST = 200  # the address dnsmasq hands the restored guest

# Dedicated, isolated range for the sandbox leg's DHCP fallback when no guest IP
# is known at provision. It MUST NOT overlap the management subnet: the sandbox
# leg lives on the isolated bridge and the management leg on the real interface,
# so sharing one subnet gives the helper two interfaces in the same /24 — Linux
# ARP flux + ambiguous egress — and the management leg (where the app reaches the
# agent) becomes intermittently unreachable ("No route to host"). RFC 6598 CGNAT
# space is reserved and practically never seen on a LAN or in a guest subnet.
SANDBOX_FALLBACK_CIDR = "100.64.0.0/24"


def sandbox_addrs(mgmt_ip: str, mgmt_prefix: int) -> tuple[str, str, str]:
    """Fixed sandbox addresses in the management subnet: the helper's own
    sandbox-leg IP (host .254) and the single IP dnsmasq hands the restored guest
    (host .200), plus the netmask.

    DEPRECATED for the sandbox leg — kept for reference. Putting the sandbox leg
    in the management subnet collides the helper's two legs (see
    ``sandbox_fallback_addrs``); the fallback now uses the isolated
    ``SANDBOX_FALLBACK_CIDR`` instead."""
    import ipaddress

    net = ipaddress.ip_network(f"{mgmt_ip}/{mgmt_prefix}", strict=False)
    return (
        str(net.network_address + SANDBOX_HELPER_HOST),
        str(net.network_address + SANDBOX_GUEST_HOST),
        str(net.netmask),
    )


def sandbox_fallback_addrs(cidr: str = SANDBOX_FALLBACK_CIDR) -> tuple[str, str, int]:
    """``(helper_sandbox_ip, dhcp_guest_ip, prefix)`` for the sandbox leg when no
    guest IP is known at provision — in the dedicated isolated ``cidr``, NEVER the
    management subnet (that overlap is what made the management leg unreachable).
    Helper takes host .254, dnsmasq hands the guest .200; only one env is live at
    a time, so the fixed hosts never collide."""
    import ipaddress

    net = ipaddress.ip_network(cidr, strict=False)
    return (
        str(net.network_address + SANDBOX_HELPER_HOST),
        str(net.network_address + SANDBOX_GUEST_HOST),
        net.prefixlen,
    )


def dnsmasq_config(guest_ip: str, netmask: str) -> str:
    """dnsmasq config that hands the restored guest ONE management-range IP on the
    sandbox leg — not tied to the guest's own config or MAC. ``interface=sbx`` +
    ``bind-interfaces`` keep DHCP on the isolated sandbox leg only (never the real
    management network). A one-address dynamic range means whatever NIC asks first
    gets ``guest_ip``, which we then reach via a /32 route on the sandbox leg."""
    return (
        "\n".join(
            [
                "# generated by backup-sentinel restore helper",
                "interface=sbx",
                "bind-interfaces",
                "except-interface=lo",
                "no-resolv",
                f"dhcp-range={guest_ip},{guest_ip},{netmask},infinite",
            ]
        )
        + "\n"
    )


def helper_legs(
    bridge: str,
    vlan_tag: int,
    mgmt_iface: str,
    mgmt_ip: str,
    mgmt_prefix: int,
    mgmt_vlan: int | None,
    mgmt_gateway: str,
    sandbox_ip: str,
    sandbox_prefix: int,
) -> dict[str, str]:
    """The helper's two network legs. net1 (mgmt, on the real interface) carries
    the management IP + gateway so the app can reach the agent; net0 (sbx, on the
    sandbox VLAN) carries the helper's own address in the GUEST's subnet (guest
    IP + 1, with the guest's own prefix — networks are not always /24) so the
    helper can serve DHCP and probe the guest on the isolated L2. For a guest with
    no known IP the sandbox address falls back to a fixed management-range host.

    The management leg gets a stable, IP-derived MAC so that a run reusing a
    management IP keeps the same MAC and the upstream gateway's ARP entry stays
    valid (a fresh random MAC each time got black-holed until the stale ARP
    expired — intermittently unreachable)."""
    mgmt_net = f"name=mgmt,bridge={mgmt_iface},hwaddr={_stable_mac(mgmt_ip)},ip={mgmt_ip}/{mgmt_prefix}"
    if mgmt_gateway:
        mgmt_net += f",gw={mgmt_gateway}"
    if mgmt_vlan:
        mgmt_net += f",tag={mgmt_vlan}"
    net0 = f"name=sbx,bridge={bridge},tag={vlan_tag},ip={sandbox_ip}/{sandbox_prefix}"
    return {"net0": net0, "net1": mgmt_net}


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
