"""Pure-logic tests for source matching and snapshot selection."""

from app.services.restore_match import (
    PbsTuple,
    is_auto_restorable,
    match_source,
    normalize,
    pick_latest_complete,
)


def _store(pbs, datastore, namespace, storage="local-pbs"):
    return {"pbs": pbs, "datastore": datastore, "namespace": namespace, "storage": storage}


def test_exact_tuple_match():
    vm_locs = [normalize("PBS-A", "store1", "cluster-x/prod")]
    stores = [_store("pbs-a", "store1", "cluster-x/prod", storage="s1")]
    match = match_source(vm_locs, stores)
    assert match is not None
    assert match["storage"] == "s1"


def test_namespace_mismatch_is_no_match():
    vm_locs = [normalize("pbs-a", "store1", "cluster-x/prod")]
    stores = [_store("pbs-a", "store1", "cluster-x/staging")]
    assert match_source(vm_locs, stores) is None
    assert is_auto_restorable(vm_locs, stores) is False


def test_pbs_name_mismatch_is_no_match():
    vm_locs = [normalize("pbs-a", "store1", "")]
    stores = [_store("pbs-b", "store1", "")]
    assert match_source(vm_locs, stores) is None


def test_normalize_is_case_and_slash_insensitive_for_ns():
    assert normalize("PBS", "ds", "/a//b/") == PbsTuple("pbs", "ds", "a/b")


def test_pick_latest_complete_skips_incomplete():
    snaps = [
        {"backup_time": 100, "complete": True, "snapshot_id": "old"},
        {"backup_time": 300, "complete": False, "snapshot_id": "newest-incomplete"},
        {"backup_time": 200, "complete": True, "snapshot_id": "newest-complete"},
    ]
    assert pick_latest_complete(snaps)["snapshot_id"] == "newest-complete"


def test_pick_latest_complete_none_when_all_incomplete():
    assert pick_latest_complete([{"backup_time": 1, "complete": False}]) is None
    assert pick_latest_complete([]) is None
