from app.services.restore_orchestrator import (
    capable_target_storages,
    choose_target_storage,
    storage_accepts_guest,
)

GB = 1024**3


def test_storage_accepts_guest_by_content():
    assert storage_accepts_guest("images,rootdir", "lxc")
    assert storage_accepts_guest("rootdir", "lxc")
    assert not storage_accepts_guest("images", "lxc")
    assert storage_accepts_guest("images,rootdir", "qemu")
    assert not storage_accepts_guest("rootdir", "qemu")


def test_capable_targets_filter_by_guest_and_keep_order():
    targets = [
        {"storage": "img-only", "use": True, "order": 0},
        {"storage": "ct-store", "use": True, "order": 1},
        {"storage": "both", "use": True, "order": 2},
        {"storage": "unticked", "use": False, "order": 3},
    ]
    content = {"img-only": "images", "ct-store": "rootdir", "both": "images,rootdir", "unticked": "rootdir"}
    assert capable_target_storages(targets, content, "lxc") == ["ct-store", "both"]
    assert capable_target_storages(targets, content, "qemu") == ["img-only", "both"]


def _caps(order, avail, total):
    """Helper: build (ordered, avail_by, total_by) from parallel lists."""
    avail_by = {s: a for s, a in zip(order, avail)}
    total_by = {s: t for s, t in zip(order, total)}
    return order, avail_by, total_by


def test_prefers_first_storage_when_it_fits():
    order, avail, total = _caps(["fast", "bulk"], [100 * GB, 100 * GB], [200 * GB, 200 * GB])
    assert choose_target_storage(order, avail, total, 40 * GB) == ("ok", "fast")


def test_falls_back_to_next_storage_that_fits():
    # Preferred (fast) has no room; bulk does → use bulk, not the preferred.
    order, avail, total = _caps(["fast", "bulk"], [10 * GB, 100 * GB], [200 * GB, 200 * GB])
    assert choose_target_storage(order, avail, total, 40 * GB) == ("ok", "bulk")


def test_transiently_full_retries_on_top():
    # Both full right now, but both have the capacity → retry later on the top one.
    order, avail, total = _caps(["fast", "bulk"], [5 * GB, 5 * GB], [200 * GB, 200 * GB])
    assert choose_target_storage(order, avail, total, 40 * GB) == ("retry", "fast")


def test_too_big_for_every_storage_capacity():
    # Guest is larger than either storage's total capacity → distribution needed.
    order, avail, total = _caps(["fast", "bulk"], [5 * GB, 5 * GB], [50 * GB, 80 * GB])
    assert choose_target_storage(order, avail, total, 120 * GB) == ("toobig", "")


def test_unknown_capacity_is_optimistic_retry_not_error():
    order, avail, total = _caps(["fast"], [1 * GB], [None])
    assert choose_target_storage(order, avail, total, 120 * GB) == ("retry", "fast")


def test_unknown_size_uses_preferred():
    order, avail, total = _caps(["fast", "bulk"], [1 * GB, 1 * GB], [50 * GB, 50 * GB])
    assert choose_target_storage(order, avail, total, None) == ("ok", "fast")
