"""LXC-specific restore helpers (pure).

Containers differ from QEMU VMs in three places the restore pipeline touches:
their `netN` config format carries the IP directly (no guest agent), the sandbox
net rewrite keeps `hwaddr`/`ip` while repointing the bridge, and host bind-mounts
must be dropped before an isolated boot. These helpers are pure so they are unit
tested without a PVE host.
"""

from __future__ import annotations

import re
from typing import Any

_MAC_RE = re.compile(r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})")


def _fields(value: str) -> dict[str, str]:
    """The comma-separated ``k=v`` fields of one config line, in order."""
    out: dict[str, str] = {}
    for part in value.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def parse_lxc_net(value: str) -> dict[str, Any]:
    """``{mac, ip, prefix}`` from one LXC ``netN`` line. ``ip``/``prefix`` are set
    only for a static CIDR (``ip=10.0.0.5/24``); ``dhcp``/``manual``/absent → None."""
    fields = _fields(value)
    mac_match = _MAC_RE.search(value)
    mac = mac_match.group(1).lower() if mac_match else None
    ip: str | None = None
    prefix: int | None = None
    raw_ip = fields.get("ip", "")
    if "/" in raw_ip:
        addr, _, pfx = raw_ip.partition("/")
        try:
            prefix = int(pfx)
            ip = addr
        except ValueError:
            ip, prefix = None, None
    return {"mac": mac, "ip": ip, "prefix": prefix}


def rewrite_lxc_net_line(value: str, bridge: str, tag: int, trunks: int | None = None) -> str:
    """Repoint an LXC ``netN`` to the sandbox bridge + VLAN, keeping ``hwaddr``/
    ``ip``/``gw``/``name`` and dropping the old bridge/tag/firewall. The QEMU
    ``rewrite_net_line`` already does exactly this for the shared comma-kv format
    (its VirtIO-only ``mtu`` branch never triggers for a container's ``name=…``
    first field), so we delegate to keep one rewrite. Imported locally so this
    pure module never imports the heavy ``restore_server`` at load time."""
    from app.services.restore_server import rewrite_net_line

    return rewrite_net_line(value, bridge, tag, trunks=trunks)


def bind_mount_deletes(config: dict[str, Any]) -> list[str]:
    """The ``mpN`` keys that are **host bind-mounts** (source is a host path, not a
    ``storage:volid``), ordered by index — to delete before an isolated boot. A
    bind-mount's host path is not in the backup and must not be reconstructed;
    volume-backed mountpoints restore normally and are kept."""
    out: list[tuple[int, str]] = []
    for key, value in config.items():
        m = re.fullmatch(r"mp(\d+)", key)
        if not m or not isinstance(value, str):
            continue
        source = value.split(",", 1)[0].strip()
        if source.startswith("/"):  # host path → bind-mount
            out.append((int(m.group(1)), key))
    return [key for _, key in sorted(out)]


def lxc_config_nics(config: dict[str, Any]) -> list[tuple[str, int]]:
    """``[(ip, prefix)]`` for each ``netN`` with a static CIDR, ordered by index."""
    nets: list[tuple[int, str]] = []
    for key, value in config.items():
        m = re.fullmatch(r"net(\d+)", key)
        if m and isinstance(value, str):
            nets.append((int(m.group(1)), value))
    out: list[tuple[str, int]] = []
    for _, value in sorted(nets):
        parsed = parse_lxc_net(value)
        if parsed["ip"] and parsed["prefix"]:
            out.append((parsed["ip"], parsed["prefix"]))
    return out
