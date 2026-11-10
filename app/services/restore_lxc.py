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
