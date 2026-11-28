"""How much room each guest's backups take on a PBS datastore.

PBS has no figure for it: chunks are shared between all backups of a
datastore. What says which chunks a snapshot consists of are its index files
(.fidx for a disk image, .didx for a file archive) — a header and one digest
per chunk. Read once per snapshot, they tell per guest

- how many different chunks its snapshots need (what they take), and
- how many of those no other guest of the datastore uses (what is freed
  when its backups go).

The counts are exact. The size is not: how large a chunk is on disk stands
only at the chunk itself, so a count is multiplied with the mean chunk size
the datastore's last garbage collection found.

Cost on the PBS: one download of each snapshot's index files (about 800 KB
per 100 GB of disk), once. No chunk is opened. It needs the right to read the
datastore (role DatastoreReader); with Audit alone PBS refuses the download.
"""

from __future__ import annotations

import logging
from array import array
from bisect import bisect_left
from collections import Counter
from itertools import compress
from typing import Any
from urllib import parse

from app import db
from app.services.pbs import PBSSyncError, _pbs_get_bytes, _pbs_get_payload

logger = logging.getLogger(__name__)

Group = tuple[str, str, str, str]  # store, namespace, backup type, backup id

# Index bytes downloaded per PBS and sync: the snapshots of a newly registered
# PBS are worked off over several syncs instead of in one burst.
INDEX_BYTES_PER_SYNC = 256 * 1024 * 1024
# Digests counted at a time when looking for shared chunks (bounds memory).
_COUNT_AT_ONCE = 400_000

# pbs-datastore/src/file_formats.rs
FIDX_MAGIC = bytes([47, 127, 65, 237, 145, 253, 15, 205])
DIDX_MAGIC = bytes([28, 145, 78, 165, 25, 186, 179, 205])
_HEADER = 4096
_FIXED_CHUNK = 4 * 1024 * 1024

_FLAGS = bytes.maketrans(b"01", b"\x00\x01")


def index_digests(data: bytes) -> set[int]:
    """The chunks an index file lists, each as the first 8 bytes of its digest.

    .fidx: 4096 bytes header, then 32 bytes digest per chunk.
    .didx: 4096 bytes header, then per chunk 8 bytes end offset + 32 bytes digest.
    """
    if len(data) < _HEADER:
        raise ValueError("index file shorter than its header")
    body = memoryview(data)[_HEADER:]
    if data[:8] == FIDX_MAGIC and len(body) % 32 == 0:
        return set(body.cast("Q")[::4])
    if data[:8] == DIDX_MAGIC and len(body) % 40 == 0:
        return set(body.cast("Q")[1::5])
    raise ValueError("not a PBS chunk index")


def _marks(bitmap: int, length: int) -> bytes:
    """A bitmap as one byte ("0"/"1") per position, lowest first."""
    return bin(bitmap)[2:].zfill(length)[::-1].encode()


class GroupChunks:
    """The chunks of one guest: every digest once, and per snapshot a bitmap
    of the ones it uses — so a pruned snapshot can be taken out again."""

    def __init__(self, digests: bytes = b"", bitmaps: dict[int, bytes] | None = None) -> None:
        self.digests = array("Q")
        self.digests.frombytes(digests)
        self.bitmaps = {time: int.from_bytes(raw, "little") for time, raw in (bitmaps or {}).items()}
        self._position: dict[int, int] | None = None

    def add(self, backup_time: int, digests: set[int]) -> None:
        if self._position is None:
            self._position = {digest: i for i, digest in enumerate(self.digests)}
        for digest in digests - self._position.keys():
            self._position[digest] = len(self.digests)
            self.digests.append(digest)
        marks = bytearray(b"0" * len(self.digests))
        for digest in digests:
            marks[self._position[digest]] = 49  # "1"
        self.bitmaps[backup_time] = int(bytes(marks[::-1]), 2) if marks else 0

    def compact(self) -> None:
        """Drop the chunks no snapshot uses any more."""
        length = len(self.digests)
        used = 0
        for bitmap in self.bitmaps.values():
            used |= bitmap
        if used.bit_count() == length:
            return
        keep = _marks(used, length).translate(_FLAGS)
        self.digests = array("Q", compress(self.digests, keep))
        self._position = None
        for backup_time, bitmap in self.bitmaps.items():
            marks = bytes(compress(_marks(bitmap, length), keep))
            self.bitmaps[backup_time] = int(marks[::-1], 2) if marks else 0

    def stored(self) -> tuple[bytes, int, dict[int, bytes]]:
        size = (len(self.digests) + 7) // 8
        return (
            self.digests.tobytes(),
            len(self.digests),
            {time: bitmap.to_bytes(size, "little") for time, bitmap in self.bitmaps.items()},
        )


def exclusive_counts(groups: dict[Any, bytes]) -> tuple[dict[Any, int], int]:
    """Per guest how many of its chunks no other guest has, and how many
    different chunks all guests use together. Counted in slices of the digest
    range, so memory stays bounded on a large datastore."""
    digests: dict[Any, array] = {}
    for key, raw in groups.items():
        values = array("Q")
        values.frombytes(raw)
        digests[key] = array("Q", sorted(values))
    slices = max(1, sum(len(values) for values in digests.values()) // _COUNT_AT_ONCE)
    start = dict.fromkeys(digests, 0)
    result = dict.fromkeys(digests, 0)
    different = 0
    for number in range(1, slices + 1):
        bound = (number << 64) // slices
        part: dict[Any, array] = {}
        for key, values in digests.items():
            end = len(values) if number == slices else bisect_left(values, bound, start[key])
            part[key] = values[start[key] : end]
            start[key] = end
        seen: Counter[int] = Counter()
        for values in part.values():
            seen.update(values)
        different += len(seen)
        once = {digest for digest, count in seen.items() if count == 1}
        for key, values in part.items():
            result[key] += sum(map(once.__contains__, values))
    return result, different


def canonical_ids(conns: list[dict[str, Any]]) -> dict[int, int]:
    """A PBS registered for several clusters is one PBS: every registration's
    id mapped to the lowest one, under which its snapshots are kept — so its
    index files are read once, not once per cluster."""
    by_url: dict[str, int] = {}
    for conn in sorted(conns, key=lambda c: c["id"]):
        by_url.setdefault(str(conn.get("api_url") or "").rstrip("/").lower(), conn["id"])
    return {conn["id"]: by_url[str(conn.get("api_url") or "").rstrip("/").lower()] for conn in conns}


def _download(conn: dict[str, Any], group: Group, backup_time: int, archive: str) -> bytes:
    store, ns, backup_type, backup_id = group
    query = {"backup-type": backup_type, "backup-id": backup_id, "backup-time": str(backup_time), "file-name": archive}
    if ns:
        query["ns"] = ns
    return _pbs_get_bytes(conn, f"/api2/json/admin/datastore/{parse.quote(store, safe='')}/download", query=query)


def _gc_counts(conn: dict[str, Any], store: str) -> tuple[int | None, int | None]:
    """Bytes and number of the chunks on disk, as the last garbage collection
    counted them (None: never ran, or not readable)."""
    try:
        data = _pbs_get_payload(conn, f"/api2/json/admin/datastore/{parse.quote(store, safe='')}/gc", retries=0).get(
            "data"
        )
    except PBSSyncError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    try:
        disk_bytes, disk_chunks = int(data.get("disk-bytes") or 0), int(data.get("disk-chunks") or 0)
    except (TypeError, ValueError):
        return None, None
    return (disk_bytes, disk_chunks) if disk_bytes > 0 and disk_chunks > 0 else (None, None)


def collect(conn: dict[str, Any], listed: dict[tuple[str, str], list[dict[str, Any]]]) -> int:
    """Remember the snapshots just listed on this PBS (`listed`: per datastore
    and namespace read in full) and read the index files of those not read
    yet. Returns how many snapshots were read."""
    pbs_id = canonical_ids(db.list_pbs_connections()).get(conn["id"], conn["id"])
    db.replace_pbs_snapshots(pbs_id, listed)
    recount = db.forget_pbs_chunks(pbs_id)  # guests a snapshot was pruned from
    indexed = db.pbs_indexed_snapshots(pbs_id)
    pending: dict[Group, list[dict[str, Any]]] = {}
    for (store, ns), rows in listed.items():
        for row in rows:
            group = (store, ns, row["backup_type"], row["backup_id"])
            if row.get("archives") and (*group, row["backup_time"]) not in indexed:
                pending.setdefault(group, []).append(row)

    budget = INDEX_BYTES_PER_SYNC
    read = 0
    denied: set[str] = set()
    readable: set[str] = set()
    changed = {group[0] for group in recount}
    for group in sorted(set(pending) | recount):
        store = group[0]
        if group not in recount and (budget <= 0 or store in denied):
            continue
        chunks = GroupChunks(*db.load_pbs_group_chunks(pbs_id, group))
        added = False
        for row in sorted(pending.get(group, []), key=lambda r: r["backup_time"]):
            if budget <= 0 or store in denied:
                break
            try:
                digests: set[int] = set()
                for archive in row["archives"]:
                    data = _download(conn, group, row["backup_time"], archive)
                    budget -= len(data)
                    digests |= index_digests(data)
            except PBSSyncError as exc:
                if exc.status in (401, 403):
                    denied.add(store)
                    logger.info("PBS %s: reading index files of %s refused", conn.get("name"), store)
                else:  # e.g. pruned since it was listed
                    logger.info("PBS %s: index of %s/%s not read: %s", conn.get("name"), group, row["backup_time"], exc)
                continue
            except ValueError as exc:
                logger.warning("PBS %s: index of %s/%s: %s", conn.get("name"), group, row["backup_time"], exc)
                continue
            chunks.add(row["backup_time"], digests)
            readable.add(store)
            added = True
            read += 1
        if added or group in recount:
            chunks.compact()
            db.save_pbs_group_chunks(pbs_id, group, *chunks.stored())
            changed.add(store)

    stores = sorted({store for store, _ns in listed})
    for store in stores:
        state = True if store in denied else (False if store in readable else None)
        db.save_pbs_store_space(pbs_id, store, *_gc_counts(conn, store), state)
        # Counted again when a guest's chunks changed — or never were in full.
        if store in changed or (db.get_pbs_store_space(pbs_id, store) or {}).get("counted_chunks") is None:
            own, different = exclusive_counts(db.pbs_store_digests(pbs_id, store))
            db.save_pbs_exclusive_chunks(pbs_id, own)
            db.save_pbs_counted_chunks(pbs_id, store, different)
    return read


def usage(pbs_id: int, store: str) -> dict[str, Any]:
    """The guests with backups on this datastore and what they take, largest
    first: `bytes` for all chunks their snapshots need, `exclusive_bytes` for
    those no other guest shares (None while not counted)."""
    canon = canonical_ids(db.list_pbs_connections())
    pbs_id = canon.get(pbs_id, pbs_id)
    space = db.get_pbs_store_space(pbs_id, store) or {}
    disk_bytes, disk_chunks = space.get("disk_bytes"), space.get("disk_chunks")
    # Mean size of a chunk on disk; before the first garbage collection only
    # the uncompressed size of a chunk is known.
    chunk_bytes = disk_bytes / disk_chunks if disk_bytes and disk_chunks else None
    per_chunk = chunk_bytes or _FIXED_CHUNK
    groups: list[dict[str, Any]] = []
    for row in db.pbs_space_groups(pbs_id, store):
        chunks, exclusive = row["chunks"], row["exclusive"]
        groups.append(
            {
                "ns": row["ns"],
                "backup_type": row["backup_type"],
                "backup_id": row["backup_id"],
                "name": row["name"] or "",
                "snapshots": row["snapshots"],
                "known": row["known"],
                "chunks": chunks,
                "bytes": int(chunks * per_chunk) if chunks is not None else None,
                "exclusive_bytes": int(exclusive * per_chunk) if exclusive is not None else None,
            }
        )
    groups.sort(key=lambda g: (-(g["bytes"] or 0), g["ns"], g["backup_id"]))
    counted = space.get("counted_chunks")
    capacity = db.pbs_store_capacity([conn_id for conn_id, same in canon.items() if same == pbs_id], store) or {}
    return {
        "groups": groups,
        # Every chunk of these guests once: what they take together.
        "counted_bytes": int(counted * per_chunk) if counted is not None else None,
        "used_bytes": capacity.get("used_bytes"),
        "avail_bytes": capacity.get("avail_bytes"),
        "snapshots": sum(g["snapshots"] for g in groups),
        "known": sum(g["known"] for g in groups),
        "chunk_bytes": int(chunk_bytes) if chunk_bytes else None,
        "denied": bool(space.get("denied")),
    }
