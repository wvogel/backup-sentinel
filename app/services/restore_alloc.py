"""Allocate a restore environment's resources from configured ranges.

Each live environment (active run or held run) owns two VMIDs (the restored VM
and its helper), one sandbox VLAN tag, and one static management IP. The set of
in-use values is derived from the ``running``/``held`` runs (spec §14), so the
DB rows *are* the in-use registry — no separate table.

Management-IP exhaustion is the gating resource: if no management IP is free,
``allocate`` returns ``None`` and the caller must wait (spec §15). VMID/VLAN
exhaustion (ranges mis-sized) also returns ``None``.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Iterator
from typing import Any, NamedTuple


class Allocation(NamedTuple):
    restore_vmid: int
    helper_vmid: int
    vlan_tag: int
    mgmt_ip: str


def ip_range(ip_from: str, ip_to: str) -> list[str]:
    """Inclusive list of IPv4 addresses from ``ip_from`` to ``ip_to``."""
    start = int(ipaddress.IPv4Address(ip_from))
    end = int(ipaddress.IPv4Address(ip_to))
    if end < start:
        return []
    return [str(ipaddress.IPv4Address(i)) for i in range(start, end + 1)]


def _lowest_free(candidates: Iterable[Any], in_use: set[Any]) -> Any | None:
    for c in candidates:
        if c not in in_use:
            return c
    return None


def _two_lowest_free(candidates: Iterator[int], in_use: set[int]) -> tuple[int, int] | None:
    picked: list[int] = []
    for c in candidates:
        if c not in in_use:
            picked.append(c)
            if len(picked) == 2:
                return picked[0], picked[1]
    return None


def allocate(
    *,
    vmid_from: int,
    vmid_to: int,
    vlan_from: int,
    vlan_to: int,
    ip_from: str,
    ip_to: str,
    owned: list[dict[str, Any]],
) -> Allocation | None:
    """Return a free ``Allocation`` or ``None`` if a required resource is
    exhausted (caller waits). ``owned`` is the list of running/held run rows."""
    in_use_vmids = {v for r in owned for v in (r.get("restore_vmid"), r.get("helper_vmid")) if v is not None}
    in_use_tags = {r["vlan_tag"] for r in owned if r.get("vlan_tag") is not None}
    in_use_ips = {r["mgmt_ip"] for r in owned if r.get("mgmt_ip") is not None}

    vmids = _two_lowest_free(iter(range(vmid_from, vmid_to + 1)), in_use_vmids)
    if vmids is None:
        return None
    vlan = _lowest_free(range(vlan_from, vlan_to + 1), in_use_tags)
    if vlan is None:
        return None
    mgmt = _lowest_free(ip_range(ip_from, ip_to), in_use_ips)
    if mgmt is None:
        return None
    return Allocation(restore_vmid=vmids[0], helper_vmid=vmids[1], vlan_tag=vlan, mgmt_ip=mgmt)
