from app.services.restore_orchestrator import choose_target_storage

GB = 1024**3


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
