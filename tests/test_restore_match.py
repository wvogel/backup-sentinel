"""Pure-logic tests for source matching and snapshot selection."""

from app.services.restore_match import is_auto_restorable, match_source, pick_latest_complete


def _entry(location, storage="local-pbs"):
    return {"location": location, "storage": storage, "pbs": "x", "datastore": "ds", "namespace": ""}


def test_exact_location_match():
    vm_locs = {"pbs:pbs-a/store1/cluster-x/prod"}
    entries = [_entry("pbs:pbs-a/store1/cluster-x/prod", storage="s1")]
    match = match_source(vm_locs, entries)
    assert match is not None
    assert match["storage"] == "s1"
    assert is_auto_restorable(vm_locs, entries) is True


def test_namespace_mismatch_is_no_match():
    vm_locs = {"pbs:pbs-a/store1/cluster-x/prod"}
    entries = [_entry("pbs:pbs-a/store1/cluster-x/staging")]
    assert match_source(vm_locs, entries) is None
    assert is_auto_restorable(vm_locs, entries) is False


def test_server_mismatch_is_no_match():
    vm_locs = {"pbs:pbs-a/store1"}
    entries = [_entry("pbs:pbs-b/store1")]
    assert match_source(vm_locs, entries) is None


def test_matches_any_of_several_vm_locations():
    # A VM with an on-site and an offsite copy matches whichever the host has.
    vm_locs = {"pbs:pbs-onsite/ds", "pbs:pbs-offsite/ds"}
    entries = [_entry("pbs:pbs-offsite/ds", storage="off")]
    assert match_source(vm_locs, entries)["storage"] == "off"


def test_no_locations_no_match():
    assert match_source(set(), [_entry("pbs:pbs-a/ds")]) is None


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
