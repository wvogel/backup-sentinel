from app.services.restore_pack import pack_disks, plan_distribution

GB = 1024**3


def test_empty_disks_pack_trivially():
    assert pack_disks([], [("x", 10 * GB)]) == {}


def test_single_disk_prefers_earlier_storage():
    assert pack_disks([("scsi0", 10 * GB)], [("fast", 100 * GB), ("bulk", 100 * GB)]) == {"scsi0": "fast"}


def test_two_disks_split_when_neither_storage_holds_both():
    # 60 + 60 on two 100-GB storages: both can't share one → one each.
    out = pack_disks([("scsi0", 60 * GB), ("scsi1", 60 * GB)], [("fast", 100 * GB), ("bulk", 100 * GB)])
    assert out == {"scsi0": "fast", "scsi1": "bulk"}


def test_backtracks_to_find_a_fit():
    # Greedy-by-first could trap the big disk; exhaustive search still fits all.
    disks = [("a", 70 * GB), ("b", 40 * GB), ("c", 40 * GB)]
    storages = [("x", 80 * GB), ("y", 80 * GB)]
    out = pack_disks(disks, storages)
    assert out is not None
    # every disk placed, no storage overfilled
    load: dict[str, int] = {}
    for dev, st in out.items():
        size = dict(disks)[dev]
        load[st] = load.get(st, 0) + size
    assert all(load[name] <= cap for name, cap in storages)
    assert set(out) == {"a", "b", "c"}


def test_disk_bigger_than_every_storage_is_unpackable():
    assert pack_disks([("scsi0", 200 * GB)], [("x", 100 * GB), ("y", 150 * GB)]) is None


def test_sum_exceeds_total_capacity_is_unpackable():
    assert pack_disks([("a", 80 * GB), ("b", 80 * GB)], [("x", 100 * GB), ("y", 50 * GB)]) is None


def test_plan_distribution_ok_uses_free_space():
    disks = [{"dev": "scsi0", "size_bytes": 60 * GB}, {"dev": "scsi1", "size_bytes": 60 * GB}]
    avail = [("fast", 70 * GB), ("bulk", 70 * GB)]
    total = [("fast", 200 * GB), ("bulk", 200 * GB)]
    decision, mapping = plan_distribution(disks, avail, total)
    assert decision == "ok"
    assert mapping == {"scsi0": "fast", "scsi1": "bulk"}


def test_plan_distribution_retry_when_only_capacity_fits():
    # No room free right now, but capacities could hold it → transient, retry.
    disks = [{"dev": "scsi0", "size_bytes": 60 * GB}, {"dev": "scsi1", "size_bytes": 60 * GB}]
    avail = [("fast", 5 * GB), ("bulk", 5 * GB)]
    total = [("fast", 200 * GB), ("bulk", 200 * GB)]
    assert plan_distribution(disks, avail, total) == ("retry", None)


def test_plan_distribution_impossible_when_capacity_too_small():
    disks = [{"dev": "scsi0", "size_bytes": 300 * GB}]
    avail = [("fast", 5 * GB), ("bulk", 5 * GB)]
    total = [("fast", 100 * GB), ("bulk", 150 * GB)]
    assert plan_distribution(disks, avail, total) == ("impossible", None)
