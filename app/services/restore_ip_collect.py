"""Best-effort collection of a guest's MAC/IP from production (spec §9).

Read-only: queries the QEMU guest agent for live IPs and the VM config for the
NIC MACs (so the sandbox DHCP can hand the guest its angestammte IP by MAC).
Missing agent / privilege → no IPs for that VM (it will restore-test as
``limited``). The parsing is pure and injectable (``pve_get``) for testing; the
sync wiring lives in `app.web.sync`.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# net0=virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,tag=5
_MAC_RE = re.compile(r"\b([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\b")


def macs_from_config(config: dict[str, Any]) -> dict[int, str]:
    """{nic_index: mac_lower} from a VM/CT config's netN entries."""
    out: dict[int, str] = {}
    for key, value in config.items():
        m = re.fullmatch(r"net(\d+)", key)
        if not m or not isinstance(value, str):
            continue
        mac = _MAC_RE.search(value)
        if mac:
            out[int(m.group(1))] = mac.group(1).lower()
    return out


def _v4_addrs(iface: dict[str, Any]) -> list[tuple[str, int | None]]:
    addrs = []
    for a in iface.get("ip-addresses", []) or []:
        ip = a.get("ip-address", "")
        if a.get("ip-address-type") != "ipv4":
            continue
        try:
            parsed = ipaddress.IPv4Address(ip)
        except ipaddress.AddressValueError:
            continue
        if parsed.is_loopback or parsed.is_link_local:
            continue
        addrs.append((ip, a.get("prefix")))
    return addrs


def nics_from_agent(agent_result: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """[{mac, ip_addresses, prefix}] from a guest-agent network-get-interfaces result."""
    nics = []
    for iface in agent_result or []:
        mac = (iface.get("hardware-address") or "").lower()
        if not mac or mac == "00:00:00:00:00:00":
            continue
        v4 = _v4_addrs(iface)
        if not v4:
            continue
        nics.append({"mac": mac, "ip_addresses": [ip for ip, _ in v4], "prefix": v4[0][1]})
    return nics


def collect_for_vm(vm: dict[str, Any], pve_get: Callable[[str], Any]) -> list[dict[str, Any]]:
    """Return NIC rows [{nic_index, mac, ip_addresses, prefix, source}] for a VM.

    ``pve_get(path)`` returns the PVE API ``data`` payload (or raises). Best
    effort: any failure yields ``[]`` and is logged, never raised.
    """
    node = vm["node_name"]
    vmid = vm["vmid"]
    gtype = vm.get("guest_type", "qemu")
    try:
        config = pve_get(f"/nodes/{node}/{gtype}/{vmid}/config") or {}
        mac_to_index = {mac: idx for idx, mac in macs_from_config(config).items()}
        if gtype == "qemu":
            agent = pve_get(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces") or {}
            agent_nics = nics_from_agent(agent.get("result", []))
        else:
            agent_nics = []
    except Exception as exc:
        logger.debug("IP collection skipped for %s/%s: %s", node, vmid, exc)
        return []

    rows: list[dict[str, Any]] = []
    for seq, nic in enumerate(agent_nics):
        rows.append(
            {
                "nic_index": mac_to_index.get(nic["mac"], seq),
                "mac": nic["mac"],
                "ip_addresses": nic["ip_addresses"],
                "prefix": nic["prefix"],
                "source": "agent",
            }
        )
    rows.sort(key=lambda r: r["nic_index"])
    return rows


def primary_ip(nics: list[dict[str, Any]], override: str | None = None) -> str | None:
    """The IP the probe targets: the operator override if set, else the first
    non-loopback IPv4 on the lowest NIC index."""
    if override:
        return override
    for nic in sorted(nics, key=lambda r: r["nic_index"]):
        if nic["ip_addresses"]:
            return nic["ip_addresses"][0]
    return None


def mark_primary(nics: list[dict[str, Any]], override: str | None = None) -> list[dict[str, Any]]:
    """Return NIC rows with ``is_primary`` set on the NIC carrying the primary IP."""
    pip = primary_ip(nics, override)
    for nic in nics:
        nic["is_primary"] = pip is not None and pip in nic["ip_addresses"]
    return nics
