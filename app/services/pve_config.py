"""Parse a PVE guest config (``qm config`` / ``pct config``) into its data
disks.

PVE returns a guest's config as a flat dict whose disk-bearing keys look like
``scsi0``, ``virtio1``, ``sata0``, ``ide0``, ``efidisk0``, ``tpmstate0`` (QEMU)
or ``rootfs``, ``mp0`` (LXC). Each value is ``<storage>:<volid>,size=<N>[KMGT]``
plus extra options. We extract the target storage and the provisioned size so
the UI can show per-volume sizes and the restore planner can bin-pack disks
onto target storages.

CD-ROM and cloud-init drives (``media=cdrom``) and detached ``unusedN`` volumes
are skipped — they aren't data disks the restore footprint depends on.
"""

from __future__ import annotations

import re
from typing import Any

# Disk-bearing config keys. unused<N> is deliberately excluded.
_DISK_KEY_RE = re.compile(r"^(?:ide|sata|scsi|virtio)\d+$|^efidisk\d+$|^tpmstate\d+$|^rootfs$|^mp\d+$")

# size=<number><unit> inside the option list; PVE uses binary (1024) units with
# a single-letter suffix (K/M/G/T/P). A bare number is bytes.
_SIZE_RE = re.compile(r"(?:^|,)size=(\d+(?:\.\d+)?)\s*([KMGTP]?)", re.IGNORECASE)
_UNIT = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}

# Display order: data disks first (fast buses before slow), then the tiny
# firmware/TPM state volumes.
_BUS_ORDER = ("virtio", "scsi", "sata", "ide", "rootfs", "mp", "efidisk", "tpmstate")


def _ordinal(dev: str) -> tuple[int, int]:
    for i, prefix in enumerate(_BUS_ORDER):
        if dev.startswith(prefix):
            num = dev[len(prefix) :]
            return (i, int(num) if num.isdigit() else 0)
    return (len(_BUS_ORDER), 0)


def parse_disks(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the guest's data disks as ``[{dev, storage, size_bytes}, ...]``,
    ordered the way they should be shown. Entries without a storage-backed
    volume or without a parseable size (CD-ROMs, ``none``) are dropped."""
    disks: list[dict[str, Any]] = []
    for key, raw in (config or {}).items():
        if not isinstance(key, str) or not _DISK_KEY_RE.match(key):
            continue
        value = str(raw)
        if "media=cdrom" in value:
            continue
        first = value.split(",", 1)[0]
        if ":" not in first:
            continue  # e.g. "none"
        storage = first.split(":", 1)[0].strip()
        if not storage or storage == "none":
            continue
        match = _SIZE_RE.search(value)
        if not match:
            continue
        size_bytes = int(float(match.group(1)) * _UNIT.get(match.group(2).upper(), 1))
        if size_bytes <= 0:
            continue
        disks.append({"dev": key, "storage": storage, "size_bytes": size_bytes})
    disks.sort(key=lambda d: _ordinal(d["dev"]))
    return disks


def total_disk_bytes(disks: list[dict[str, Any]]) -> int:
    """Sum of the parsed disks' provisioned sizes."""
    return sum(int(d.get("size_bytes") or 0) for d in disks)
