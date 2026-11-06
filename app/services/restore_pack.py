"""Bin-pack a guest's disks onto several target storages.

PVE's archive restore writes the whole guest to one storage, so a guest larger
than every single target storage can only be restored by placing each disk on a
storage with room and assembling the VM afterwards (see restore_server's
per-disk restore). This module answers the planning question: *is there an
assignment of whole disks to storages that fits, and if so which?*

Each disk goes wholly onto one storage (a disk is never split). The search is
exhaustive with first-fit-decreasing ordering — disk counts are tiny, so it
always finds a packing when one exists ("in keiner Konstellation verteilbar"
truly means impossible) and prefers earlier storages in the operator's order.
"""

from __future__ import annotations

from typing import Any


def pack_disks(
    disks: list[tuple[str, int]],
    storages: list[tuple[str, int]],
) -> dict[str, str] | None:
    """Assign each disk to one storage so every storage's assigned total stays
    within its capacity.

    ``disks`` is ``[(dev, size_bytes), ...]``; ``storages`` is
    ``[(name, capacity_bytes), ...]`` in the operator's preferred order. Returns
    ``{dev: storage}`` (earlier storages preferred, backtracking when needed) or
    ``None`` when no assignment fits — including when a single disk is larger
    than every storage.
    """
    if not disks:
        return {}
    order = sorted(disks, key=lambda d: d[1], reverse=True)
    names = [name for name, _ in storages]
    remaining = {name: cap for name, cap in storages}
    result: dict[str, str] = {}

    def place(i: int) -> bool:
        if i == len(order):
            return True
        dev, size = order[i]
        for name in names:
            if remaining[name] >= size:
                remaining[name] -= size
                result[dev] = name
                if place(i + 1):
                    return True
                remaining[name] += size
                del result[dev]
        return False

    return dict(result) if place(0) else None


def plan_distribution(
    disks: list[dict[str, Any]],
    avail: list[tuple[str, int]],
    total: list[tuple[str, int]],
) -> tuple[str, dict[str, str] | None]:
    """Decide how to place a guest's disks across target storages.

    ``disks`` is ``[{dev, size_bytes}, ...]``; ``avail`` and ``total`` are
    ``[(name, bytes), ...]`` in operator order — the free space and the capacity
    of each ticked storage. Returns ``(decision, mapping)``:

    - ``("ok", {dev: storage})``  — a packing fits the free space right now.
    - ``("retry", None)``         — it doesn't fit the free space now but would
      fit the storages' capacities, so the shortfall is transient; wait.
    - ``("impossible", None)``    — no packing fits even the capacities (a disk
      bigger than every storage, or the disks together exceed total capacity).
    """
    pairs = [(d["dev"], int(d.get("size_bytes") or 0)) for d in disks]
    by_avail = pack_disks(pairs, avail)
    if by_avail is not None:
        return ("ok", by_avail)
    if pack_disks(pairs, total) is not None:
        return ("retry", None)
    return ("impossible", None)
