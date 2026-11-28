"""Space per guest on a PBS datastore, counted from the snapshots' index files."""

import json
import struct
from pathlib import Path

import pytest

from app.services import pbs_space
from app.services.pbs import PBSSyncError, _guest_snapshots


def fidx(*chunks: int) -> bytes:
    """A fixed index as PBS writes it: 4096 bytes header, 32 bytes per chunk."""
    header = pbs_space.FIDX_MAGIC + bytes(56) + struct.pack("<QQ", len(chunks) * 4194304, 4194304)
    return header.ljust(4096, b"\0") + b"".join(struct.pack("<Q", c) + bytes(24) for c in chunks)


def didx(*chunks: int) -> bytes:
    """A dynamic index: per chunk its end offset, then the digest."""
    body = b"".join(struct.pack("<QQ", (i + 1) * 1000, c) + bytes(24) for i, c in enumerate(chunks))
    return (pbs_space.DIDX_MAGIC + bytes(56)).ljust(4096, b"\0") + body


def test_an_index_file_lists_its_chunks_once():
    assert pbs_space.index_digests(fidx(7, 8, 7, 9)) == {7, 8, 9}
    # In a dynamic index the offsets are not taken for digests.
    assert pbs_space.index_digests(didx(1000, 5, 5)) == {1000, 5}
    assert pbs_space.index_digests(fidx()) == set()


@pytest.mark.parametrize("data", [b"", b"x" * 5000, fidx(1)[:-1]])
def test_what_is_no_index_is_refused(data):
    with pytest.raises(ValueError):
        pbs_space.index_digests(data)


def test_the_listing_names_the_index_files_of_a_snapshot():
    rows = [
        {
            "backup-type": "vm",
            "backup-id": "104",
            "backup-time": 100,
            "size": 1,
            "files": [
                {"filename": "drive-scsi0.img.fidx"},
                {"filename": "qemu-server.conf.blob"},
                {"filename": "index.json.blob"},
            ],
        }
    ]
    assert _guest_snapshots(rows)[0]["archives"] == ["drive-scsi0.img.fidx"]


def test_a_pruned_snapshot_takes_its_own_chunks_with_it():
    chunks = pbs_space.GroupChunks()
    chunks.add(100, {1, 2, 3})
    chunks.add(200, {2, 3, 4})
    chunks.add(300, {3, 4, 5})
    digests, count, bitmaps = chunks.stored()
    assert count == 5

    # As read back from the database, without the oldest snapshot.
    del bitmaps[100]
    later = pbs_space.GroupChunks(digests, bitmaps)
    later.compact()
    digests, count, bitmaps = later.stored()
    assert count == 4 and 1 not in pbs_space.GroupChunks(digests).digests
    # The snapshots left still name their chunks.
    again = pbs_space.GroupChunks(digests, bitmaps)
    again.bitmaps.pop(200)
    again.compact()
    assert sorted(again.digests) == [3, 4, 5]


def test_chunks_no_other_guest_has_are_a_guests_own(monkeypatch):
    from array import array

    def raw(*values):
        return array("Q", values).tobytes()

    top = 2**64 - 1
    groups = {"a": raw(1, 2, 3, top), "b": raw(3, 4, 2**63), "c": raw(3, 2**63)}
    # Six different chunks in all: 1, 2, 3, top, 4 and 2**63.
    assert pbs_space.exclusive_counts(groups) == ({"a": 3, "b": 1, "c": 0}, 6)
    # The same when counted in slices of the digest range.
    monkeypatch.setattr(pbs_space, "_COUNT_AT_ONCE", 2)
    assert pbs_space.exclusive_counts(groups) == ({"a": 3, "b": 1, "c": 0}, 6)
    assert pbs_space.exclusive_counts({}) == ({}, 0)


@pytest.fixture
def tables(monkeypatch):
    """The tables of app.db_pbs_space in memory, and a PBS serving index files."""
    state = {"snapshots": {}, "groups": {}, "bitmaps": {}, "space": {}, "files": {}, "requests": [], "gc": {}}
    conns = [{"id": 1, "api_url": "https://pbs-a:8007"}, {"id": 2, "api_url": "https://pbs-b:8007"}]
    snapshots, groups, bitmaps = state["snapshots"], state["groups"], state["bitmaps"]

    def replace(pbs_id, listed):
        for (store, ns), rows in listed.items():
            for key in [k for k in snapshots if k[:3] == (pbs_id, store, ns)]:
                del snapshots[key]
            for row in rows:
                snapshots[(pbs_id, store, ns, row["backup_type"], row["backup_id"], row["backup_time"])] = row

    def forget(pbs_id):
        lost = set()
        for key in [k for k in bitmaps if k[0] == pbs_id and k not in snapshots]:
            del bitmaps[key]
            lost.add(key[1:5])
        for key in [k for k in groups if k[0] == pbs_id and not any(s[:5] == k for s in snapshots)]:
            del groups[key]
            lost.add(key[1:])
        return lost

    def load(pbs_id, group):
        key = (pbs_id, *group)
        return groups.get(key, {}).get("digests", b""), {k[5]: v for k, v in bitmaps.items() if k[:5] == key}

    def save(pbs_id, group, digests, chunks, maps):
        key = (pbs_id, *group)
        for old in [k for k in bitmaps if k[:5] == key]:
            del bitmaps[old]
        groups.pop(key, None)
        if maps:
            groups[key] = {"digests": digests, "chunks": chunks, "exclusive": None}
            bitmaps.update({(*key, time): raw for time, raw in maps.items()})

    def exclusive(pbs_id, counts):
        for group, count in counts.items():
            groups[(pbs_id, *group)]["exclusive"] = count

    def store_space(pbs_id, store, disk_bytes, disk_chunks, denied):
        row = state["space"].setdefault(
            (pbs_id, store), {"disk_bytes": None, "disk_chunks": None, "denied": False, "counted_chunks": None}
        )
        row["disk_bytes"] = disk_bytes if disk_bytes is not None else row["disk_bytes"]
        row["disk_chunks"] = disk_chunks if disk_chunks is not None else row["disk_chunks"]
        row["denied"] = denied if denied is not None else row["denied"]

    def space_groups(pbs_id, store):
        found = {}
        for key, row in snapshots.items():
            if key[:2] != (pbs_id, store):
                continue
            group = found.setdefault(
                key[:5],
                {"ns": key[2], "backup_type": key[3], "backup_id": key[4], "snapshots": 0, "known": 0, "name": None},
            )
            group["snapshots"] += 1
            group["known"] += key in bitmaps
            group["name"] = row.get("guest_name") or group["name"]
            group["chunks"] = groups.get(key[:5], {}).get("chunks")
            group["exclusive"] = groups.get(key[:5], {}).get("exclusive")
        return list(found.values())

    db = pbs_space.db
    monkeypatch.setattr(db, "list_pbs_connections", lambda *a: conns)
    monkeypatch.setattr(db, "replace_pbs_snapshots", replace)
    monkeypatch.setattr(db, "forget_pbs_chunks", forget)
    monkeypatch.setattr(db, "pbs_indexed_snapshots", lambda pbs_id: {k[1:] for k in bitmaps if k[0] == pbs_id})
    monkeypatch.setattr(db, "load_pbs_group_chunks", load)
    monkeypatch.setattr(db, "save_pbs_group_chunks", save)
    monkeypatch.setattr(
        db,
        "pbs_store_digests",
        lambda pbs_id, store: {k[1:]: v["digests"] for k, v in groups.items() if k[:2] == (pbs_id, store)},
    )
    monkeypatch.setattr(db, "save_pbs_exclusive_chunks", exclusive)
    monkeypatch.setattr(db, "save_pbs_store_space", store_space)
    monkeypatch.setattr(db, "get_pbs_store_space", lambda pbs_id, store: state["space"].get((pbs_id, store)))
    monkeypatch.setattr(db, "pbs_space_groups", space_groups)
    monkeypatch.setattr(
        db,
        "save_pbs_counted_chunks",
        lambda pbs_id, store, chunks: state["space"][(pbs_id, store)].update(counted_chunks=chunks),
    )
    monkeypatch.setattr(db, "pbs_store_capacity", lambda pbs_ids, store: state.get("capacity"))

    def get_bytes(conn, path, query=None):
        state["requests"].append((path, dict(query)))
        key = (query.get("ns", ""), query["backup-id"], int(query["backup-time"]), query["file-name"])
        if state.get("refuse"):
            error = PBSSyncError("PBS API returned HTTP 403")
            error.status = 403
            raise error
        if key not in state["files"]:
            error = PBSSyncError("PBS API returned HTTP 400: no such file")
            error.status = 400
            raise error
        return state["files"][key]

    monkeypatch.setattr(pbs_space, "_pbs_get_bytes", get_bytes)
    monkeypatch.setattr(pbs_space, "_pbs_get_payload", lambda conn, path, **kw: {"data": state["gc"]})
    return state


def snap(vmid, time, name=None, archives=("drive-scsi0.img.fidx",)):
    return {
        "backup_type": "vm",
        "backup_id": str(vmid),
        "backup_time": time,
        "guest_name": name,
        "archives": list(archives),
    }


CONN = {"id": 1, "name": "pbs-a"}


def test_each_index_is_read_once_and_guests_are_counted(tables):
    tables["gc"] = {"disk-bytes": 2000, "disk-chunks": 2}  # a chunk takes 1000 bytes on disk
    tables["files"] = {
        ("berlin", "104", 100, "drive-scsi0.img.fidx"): fidx(1, 2, 3),
        ("berlin", "104", 200, "drive-scsi0.img.fidx"): fidx(2, 3, 4),
        ("berlin", "104", 200, "drive-scsi1.img.fidx"): fidx(9),
        ("berlin", "105", 100, "drive-scsi0.img.fidx"): fidx(3, 4, 5, 6),
    }
    listed = {
        ("nas-b1", "berlin"): [
            snap(104, 100, "web"),
            snap(104, 200, "web", ["drive-scsi0.img.fidx", "drive-scsi1.img.fidx"]),
            snap(105, 100, "db"),
        ]
    }
    assert pbs_space.collect(CONN, listed) == 3
    path, query = tables["requests"][0]
    assert path == "/api2/json/admin/datastore/nas-b1/download"
    assert query == {
        "backup-type": "vm",
        "backup-id": "104",
        "backup-time": "100",
        "file-name": "drive-scsi0.img.fidx",
        "ns": "berlin",
    }

    result = pbs_space.usage(1, "nas-b1")
    web, db_guest = result["groups"]
    # web needs the chunks 1 2 3 4 9; 3 and 4 it shares with db.
    assert (web["name"], web["chunks"], web["bytes"], web["exclusive_bytes"]) == ("web", 5, 5000, 3000)
    assert (db_guest["chunks"], db_guest["exclusive_bytes"]) == (4, 2000)
    assert (result["snapshots"], result["known"], result["chunk_bytes"], result["denied"]) == (3, 3, 1000, False)
    # Together they need 1 2 3 4 5 6 9: each chunk once.
    assert result["counted_bytes"] == 7000 and result["used_bytes"] is None

    # The next sync reads nothing again.
    before = len(tables["requests"])
    assert pbs_space.collect(CONN, listed) == 0
    assert len(tables["requests"]) == before


def test_a_pruned_snapshot_no_longer_counts(tables):
    tables["files"] = {
        ("", "104", 100, "drive-scsi0.img.fidx"): fidx(1, 2, 3),
        ("", "104", 200, "drive-scsi0.img.fidx"): fidx(3, 4),
        ("", "105", 100, "drive-scsi0.img.fidx"): fidx(1),
    }
    pbs_space.collect(CONN, {("nas-b1", ""): [snap(104, 100), snap(104, 200), snap(105, 100)]})
    assert [g["chunks"] for g in pbs_space.usage(1, "nas-b1")["groups"]] == [4, 1]

    pbs_space.collect(CONN, {("nas-b1", ""): [snap(104, 200), snap(105, 100)]})
    first, second = pbs_space.usage(1, "nas-b1")["groups"]
    assert (first["chunks"], first["snapshots"], first["known"]) == (2, 1, 1)
    # Chunk 1 is now the other guest's alone.
    assert second["exclusive_bytes"] == pbs_space._FIXED_CHUNK

    # A guest whose backups are all gone leaves the list.
    pbs_space.collect(CONN, {("nas-b1", ""): [snap(105, 100)]})
    assert [g["backup_id"] for g in pbs_space.usage(1, "nas-b1")["groups"]] == ["105"]
    assert not [key for key in tables["groups"] if key[4] == "104"]


def test_without_a_garbage_collection_a_chunk_counts_uncompressed(tables):
    tables["files"] = {("", "104", 100, "drive-scsi0.img.fidx"): fidx(1, 2)}
    pbs_space.collect(CONN, {("nas-b1", ""): [snap(104, 100)]})
    result = pbs_space.usage(1, "nas-b1")
    assert result["chunk_bytes"] is None and result["groups"][0]["bytes"] == 2 * 4194304


def test_a_token_that_may_not_read_is_named_and_not_asked_again_this_sync(tables):
    tables["refuse"] = True
    listed = {("nas-b1", ""): [snap(104, 100), snap(104, 200), snap(105, 100)]}
    assert pbs_space.collect(CONN, listed) == 0
    assert len(tables["requests"]) == 1
    result = pbs_space.usage(1, "nas-b1")
    assert result["denied"] is True and result["known"] == 0
    assert result["groups"][0]["bytes"] is None

    # Once it may, the note goes.
    tables["refuse"] = False
    tables["files"] = {("", "104", 100, "drive-scsi0.img.fidx"): fidx(1)}
    assert pbs_space.collect(CONN, listed) == 1  # the other two are not there (any more)
    assert pbs_space.usage(1, "nas-b1")["denied"] is False


def test_the_backlog_of_a_new_pbs_is_spread_over_several_syncs(tables, monkeypatch):
    tables["files"] = {("", str(vm), 100, "drive-scsi0.img.fidx"): fidx(vm) for vm in (101, 102, 103)}
    listed = {("nas-b1", ""): [snap(vm, 100) for vm in (101, 102, 103)]}
    monkeypatch.setattr(pbs_space, "INDEX_BYTES_PER_SYNC", len(fidx(1)) + 1)
    assert pbs_space.collect(CONN, listed) == 2
    assert pbs_space.usage(1, "nas-b1")["known"] == 2
    assert pbs_space.collect(CONN, listed) == 1


def test_a_pbs_registered_for_two_clusters_is_kept_once(tables):
    tables["conns"] = None
    pbs_space.db.list_pbs_connections().append({"id": 7, "api_url": "https://PBS-A:8007/"})
    pbs_space.db.replace_pbs_snapshots(1, {("nas-b1", ""): [snap(104, 100)]})
    assert pbs_space.canonical_ids(pbs_space.db.list_pbs_connections())[7] == 1
    assert pbs_space.usage(7, "nas-b1")["snapshots"] == 1


def test_the_ring_of_a_registered_pbs_opens_the_list():
    topology = Path("templates/_topology.html").read_text()
    assert 'data-pbs-space="{{ group.conn_id }}" data-store="{{ ds.store }}"' in topology
    assert "/static/js/pbs-space.js" in Path("templates/base.html").read_text()
    script = Path("static/js/pbs-space.js").read_text()
    assert "/api/pbs-space?" in script and ".innerHTML" not in script
    # Headings sort, and what the figures mean stands behind the title as a "?".
    assert 'el("div", ("sortable "' in script and "aria-sort" in script
    assert 'el("span", "help-dot", "?")' in script and "space-note" not in script
    assert 'el.closest("dialog") || document.body' in Path("static/js/common.js").read_text()
    # The circle over the list marks the rows of the part under the pointer.
    assert "space-top" in script and "createElementNS" in script and 'classList.toggle("is-hot"' in script
    for key in (
        "space.col_shared",
        "space.denied",
        "space.note_size",
        "space.note_raw",
        "space.total",
        "space.sum_used",
    ):
        assert key in script
        for lang in ("de", "en"):
            assert key in json.loads(Path(f"app/i18n/{lang}.json").read_text())


def test_the_api_adds_sizes_and_bars_from_zero(monkeypatch):
    from app.web import routes_dashboard

    gib = 1024**3
    groups = [
        {"bytes": 200 * gib, "exclusive_bytes": 150 * gib, "known": 3, "snapshots": 3},
        {"bytes": 50 * gib, "exclusive_bytes": 50 * gib, "known": 1, "snapshots": 2},
        {"bytes": None, "exclusive_bytes": None, "known": 0, "snapshots": 4},
    ]
    result = {
        "groups": groups,
        "snapshots": 9,
        "known": 4,
        "chunk_bytes": 2 * 1024**2,
        "denied": False,
        "counted_bytes": 220 * gib,
        "used_bytes": 300 * gib,
        "avail_bytes": 700 * gib,
    }
    monkeypatch.setattr(routes_dashboard.pbs_space, "usage", lambda pbs, store: result)

    data = json.loads(routes_dashboard.get_pbs_space(1, "nas-b1").body)
    assert [g["share"] for g in data["groups"]] == [100.0, 25.0, 0]
    assert [g["exclusive_share"] for g in data["groups"]] == [75.0, 25.0, 0]
    assert data["groups"][0]["size"].startswith("200") and data["groups"][0]["shared_size"].startswith("50")
    # Nothing shared and nothing known are "–", not "0".
    assert data["groups"][1]["shared_size"] == "–" and data["groups"][2]["size"] == "–"
    assert data["chunk_size"].startswith("2")

    # The circle is the datastore's whole size: guests' own, shared, other use, free.
    parts = {part["kind"] + part.get("key", ""): part for part in data["parts"]}
    assert set(parts) == {"guestg0", "guestg1", "shared", "other", "free"}
    assert parts["shared"]["bytes"] == 20 * gib and parts["other"]["bytes"] == 80 * gib
    assert parts["free"]["share"] == 70.0 and round(sum(p["share"] for p in data["parts"])) == 100
    assert (data["counted_size"][:3], data["used_size"][:3], data["avail_size"][:3]) == ("220", "300", "700")


def test_small_guests_share_one_slice_and_no_size_means_no_free_part(monkeypatch):
    from app.web import routes_dashboard

    groups = [{"bytes": 10 * n, "exclusive_bytes": 10 * n, "known": 1, "snapshots": 1} for n in range(9, 0, -1)]
    result = {"groups": groups, "snapshots": 9, "known": 9, "chunk_bytes": None, "denied": False}
    result |= {"counted_bytes": 450, "used_bytes": None, "avail_bytes": None}
    monkeypatch.setattr(routes_dashboard.pbs_space, "usage", lambda pbs, store: result)
    data = json.loads(routes_dashboard.get_pbs_space(1, "nas-b1").body)
    kinds = [part["kind"] for part in data["parts"]]
    assert kinds == ["guest"] * 6 + ["rest"]  # nothing shared: no empty slice
    assert data["parts"][-1]["count"] == 3 and data["parts"][-1]["keys"] == ["g6", "g7", "g8"]
