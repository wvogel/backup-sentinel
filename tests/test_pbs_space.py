"""Space per guest on a PBS datastore, from the backup task logs."""

import pytest

from app.services import pbs_space

# As PBS writes it (src/api2/backup/environment.rs, log_upload_stat).
LOG = """starting new backup on datastore 'nas-b1' from ::ffff:10.0.0.5: "ns/berlin/vm/104/2026-11-27T02:00:03Z"
download 'index.json.blob' from previous backup 'vm/104/2026-11-26T02:00:02Z'.
register chunks in 'drive-scsi0.img.fidx' from previous backup 'vm/104/2026-11-26T02:00:02Z'.
created new fixed index 1 ("ns/berlin/vm/104/2026-11-27T02:00:03Z/drive-scsi0.img.fidx")
add blob "/mnt/datastore/nas-b1/ns/berlin/vm/104/2026-11-27T02:00:03Z/qemu-server.conf.blob" (410 bytes, comp: 410)
Upload statistics for 'drive-scsi0.img.fidx'
UUID: 0123456789abcdef0123456789abcdef
Checksum: 00
Size: 107374182400
Chunk count: 25600
Upload size: 4194304000 (3%)
Duplicates: 24600+100 (96%)
Compression: 50%
successfully closed fixed index 1
Upload statistics for 'drive-scsi1.img.fidx'
Size: 0
Chunk count: 0
successfully finished backup
TASK OK""".split("\n")


def test_the_log_of_a_backup_task_tells_what_it_stored():
    stats = pbs_space.parse_backup_log(LOG)
    assert stats["store"] == "nas-b1" and stats["ns"] == "berlin"
    assert (stats["backup_type"], stats["backup_id"]) == ("vm", "104")
    assert stats["backup_time"] == 1795744803
    assert stats["upload_bytes"] == 4194304000
    # Half of it after compression, and 100 of the 1000 chunks sent were there already.
    assert stats["stored_bytes"] == 4194304000 // 2 * 900 // 1000


@pytest.mark.parametrize(
    ("path", "ns"),
    [
        ("vm/104/2026-11-27T02:00:03Z", ""),
        ("ns/a/ns/b/ct/104/2026-11-27T02:00:03Z", "a/b"),
        ("/mnt/datastore/store/ns/a/vm/104/2026-11-27T02:00:03Z", "a"),
    ],
)
def test_snapshot_paths(path, ns):
    assert pbs_space.parse_snapshot_path(path)["ns"] == ns


def test_a_log_without_a_snapshot_tells_nothing():
    assert pbs_space.parse_backup_log(["starting something", "TASK ERROR: nope"]) is None
    assert pbs_space.parse_snapshot_path("vm/104/not-a-time") is None


def test_a_backup_without_upload_statistics_stored_nothing():
    stats = pbs_space.parse_backup_log([*LOG[:1], "successfully finished backup"])
    assert stats["stored_bytes"] == 0 and stats["upload_bytes"] == 0


@pytest.fixture
def tables(monkeypatch):
    """pbs_snapshots / pbs_backup_tasks in memory."""
    snapshots: dict[tuple, dict] = {}
    tasks: dict[tuple, dict] = {}
    conns = [{"id": 1, "api_url": "https://pbs-a:8007"}, {"id": 2, "api_url": "https://pbs-b:8007"}]
    jobs: list[dict] = []

    def replace(pbs_id, listed):
        for (store, ns), rows in listed.items():
            for key in [k for k in snapshots if k[:3] == (pbs_id, store, ns)]:
                del snapshots[key]
            for row in rows:
                snapshots[(pbs_id, store, ns, row["backup_type"], row["backup_id"], row["backup_time"])] = row

    def stats_of(key):
        return next((t for t in tasks.values() if t.get("key") == key), None)

    def listing(pbs_id, store, ns=None):
        return [
            {
                "ns": key[2],
                "backup_type": key[3],
                "backup_id": key[4],
                "backup_time": key[5],
                "guest_name": row.get("guest_name"),
                "stored_bytes": (stats_of(key) or {}).get("stored_bytes"),
            }
            for key, row in snapshots.items()
            if key[:2] == (pbs_id, store) and (ns is None or key[2] == ns)
        ]

    def without(pbs_id):
        return [
            {"store": k[1], "backup_type": k[3], "backup_id": k[4], "backup_time": k[5]}
            for k in snapshots
            if k[0] == pbs_id and not stats_of(k)
        ]

    def save(pbs_id, upid, started, stats):
        key = None
        if stats:
            key = (pbs_id, stats["store"], stats["ns"], stats["backup_type"], stats["backup_id"], stats["backup_time"])
        tasks[(pbs_id, upid)] = {"key": key, "stored_bytes": (stats or {}).get("stored_bytes")}

    db = pbs_space.db
    monkeypatch.setattr(db, "list_pbs_connections", lambda *a: conns)
    monkeypatch.setattr(db, "list_pbs_sync_jobs", lambda: jobs)
    monkeypatch.setattr(db, "replace_pbs_snapshots", replace, raising=False)
    monkeypatch.setattr(db, "list_pbs_snapshots", listing, raising=False)
    monkeypatch.setattr(db, "pbs_snapshots_without_stats", without, raising=False)
    monkeypatch.setattr(db, "pbs_backup_tasks_read", lambda pbs_id: {u for p, u in tasks if p == pbs_id}, raising=False)
    monkeypatch.setattr(db, "save_pbs_backup_task", save, raising=False)
    monkeypatch.setattr(db, "forget_pbs_backup_tasks", lambda pbs_id: None, raising=False)
    return {"snapshots": snapshots, "tasks": tasks, "conns": conns, "jobs": jobs}


def snap(vmid, time, name=None):
    return {"backup_type": "vm", "backup_id": str(vmid), "backup_time": time, "guest_name": name}


T = 1795744803  # the snapshot of LOG


def test_each_task_log_is_read_once_and_only_for_guests_that_miss_it(tables, monkeypatch):
    asked: list[str] = []
    task_rows = [
        {
            "upid": "U1",
            "worker_type": "backup",
            "worker_id": "nas-b1:vm/104",
            "starttime": T - 2,
            "endtime": T + 60,
            "status": "OK",
        },
        {
            "upid": "U2",
            "worker_type": "backup",
            "worker_id": "nas-b1:vm/999",
            "starttime": T - 2,
            "endtime": T + 60,
            "status": "OK",
        },
        {
            "upid": "U3",
            "worker_type": "backup",
            "worker_id": "nas-b1:vm/104",
            "starttime": T - 2,
            "endtime": None,
            "status": "",
        },
    ]

    def payload(conn, path, **kwargs):
        if path.endswith("/tasks"):
            return {"data": task_rows}
        asked.append(path)
        return {"data": [{"n": i, "t": line} for i, line in enumerate(LOG, 1)]}

    monkeypatch.setattr(pbs_space, "_pbs_get_payload", payload)
    listed = {("nas-b1", "berlin"): [snap(104, T, "APP02")]}
    assert pbs_space.collect(tables["conns"][0], listed) == 1
    assert len(asked) == 1 and "U1" in asked[0]
    assert pbs_space.collect(tables["conns"][0], listed) == 0  # known now
    assert len(asked) == 1

    result = pbs_space.usage(1, "nas-b1")
    assert result["groups"] == [
        {
            "ns": "berlin",
            "backup_type": "vm",
            "backup_id": "104",
            "name": "APP02",
            "snapshots": 1,
            "known": 1,
            "stored_bytes": 4194304000 // 2 * 900 // 1000,
        }
    ]


def test_a_pruned_snapshot_no_longer_counts(tables):
    pbs_space.db.replace_pbs_snapshots(1, {("nas-b1", ""): [snap(104, 100), snap(104, 200)]})
    pbs_space.db.save_pbs_backup_task(
        1,
        "U1",
        100,
        {"store": "nas-b1", "ns": "", "backup_type": "vm", "backup_id": "104", "backup_time": 100, "stored_bytes": 70},
    )
    pbs_space.db.save_pbs_backup_task(
        1,
        "U2",
        200,
        {"store": "nas-b1", "ns": "", "backup_type": "vm", "backup_id": "104", "backup_time": 200, "stored_bytes": 5},
    )
    assert pbs_space.usage(1, "nas-b1")["stored_bytes"] == 75
    pbs_space.db.replace_pbs_snapshots(1, {("nas-b1", ""): [snap(104, 200)]})
    assert pbs_space.usage(1, "nas-b1")["stored_bytes"] == 5


def test_a_synced_copy_counts_with_what_it_took_at_its_source(tables):
    """Pulled from pbs-a/nas-b1 (namespace berlin) into pbs-b/nas-off/copies/berlin."""
    tables["jobs"].append(
        {
            "pbs_id": 2,
            "direction": "pull",
            "store": "nas-off",
            "ns": "copies",
            "remote_name": "a",
            "remote_pbs_id": 1,
            "remote_store": "nas-b1",
            "remote_ns": "",
        }
    )
    pbs_space.db.replace_pbs_snapshots(1, {("nas-b1", "berlin"): [snap(104, 100)]})
    pbs_space.db.save_pbs_backup_task(
        1,
        "U1",
        100,
        {
            "store": "nas-b1",
            "ns": "berlin",
            "backup_type": "vm",
            "backup_id": "104",
            "backup_time": 100,
            "stored_bytes": 70,
        },
    )
    pbs_space.db.replace_pbs_snapshots(
        2, {("nas-off", "copies/berlin"): [snap(104, 100), snap(104, 50)], ("nas-off", "other"): [snap(104, 100)]}
    )
    groups = {g["ns"]: g for g in pbs_space.usage(2, "nas-off")["groups"]}
    assert (groups["copies/berlin"]["known"], groups["copies/berlin"]["stored_bytes"]) == (1, 70)
    # The same guest id and time elsewhere is another cluster's guest: no figure.
    assert (groups["other"]["known"], groups["other"]["stored_bytes"]) == (0, 0)


def test_a_pbs_registered_for_two_clusters_is_kept_once(tables):
    tables["conns"].append({"id": 7, "api_url": "https://PBS-A:8007/"})
    pbs_space.db.replace_pbs_snapshots(1, {("nas-b1", ""): [snap(104, 100)]})
    assert pbs_space.canonical_ids(tables["conns"])[7] == 1
    assert pbs_space.usage(7, "nas-b1")["snapshots"] == 1


def test_the_ring_of_a_registered_pbs_opens_the_list():
    from pathlib import Path

    topology = Path("templates/_topology.html").read_text()
    assert 'data-pbs-space="{{ group.conn_id }}" data-store="{{ ds.store }}"' in topology
    assert "/static/js/pbs-space.js" in Path("templates/base.html").read_text()
    script = Path("static/js/pbs-space.js").read_text()
    assert "/api/pbs-space?" in script and ".innerHTML" not in script


def test_the_api_adds_sizes_and_a_bar_from_zero(monkeypatch):
    from app.web import routes_dashboard

    groups = [
        {"stored_bytes": 200 * 1024**3, "known": 3, "snapshots": 3},
        {"stored_bytes": 50 * 1024**3, "known": 1, "snapshots": 2},
        {"stored_bytes": 0, "known": 0, "snapshots": 4},
    ]
    result = {"groups": groups, "stored_bytes": 250 * 1024**3, "snapshots": 9, "known": 4}
    monkeypatch.setattr(routes_dashboard.pbs_space, "usage", lambda pbs, store: result)
    import json

    data = json.loads(routes_dashboard.get_pbs_space(1, "nas-b1").body)
    assert [g["share"] for g in data["groups"]] == [100.0, 25.0, 0]
    assert data["groups"][2]["size"] == "–"  # nothing known is not "0"
    assert data["size"].startswith("250")
