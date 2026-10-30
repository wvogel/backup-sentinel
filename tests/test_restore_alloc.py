"""Pure-logic tests for environment resource allocation."""

from app.services.restore_alloc import Allocation, allocate, ip_range

RANGES = dict(vmid_from=9000, vmid_to=9100, vlan_from=900, vlan_to=999, ip_from="10.9.0.10", ip_to="10.9.0.12")


def test_ip_range_inclusive():
    assert ip_range("10.9.0.10", "10.9.0.12") == ["10.9.0.10", "10.9.0.11", "10.9.0.12"]
    assert ip_range("10.9.0.12", "10.9.0.10") == []


def test_allocates_lowest_free_of_each():
    alloc = allocate(owned=[], **RANGES)
    assert alloc == Allocation(restore_vmid=9000, helper_vmid=9001, vlan_tag=900, mgmt_ip="10.9.0.10")


def test_skips_in_use_vmids_tags_ips():
    owned = [
        {"restore_vmid": 9000, "helper_vmid": 9001, "vlan_tag": 900, "mgmt_ip": "10.9.0.10"},
    ]
    alloc = allocate(owned=owned, **RANGES)
    assert alloc == Allocation(restore_vmid=9002, helper_vmid=9003, vlan_tag=901, mgmt_ip="10.9.0.11")


def test_returns_none_when_mgmt_ips_exhausted():
    owned = [
        {"restore_vmid": 9000, "helper_vmid": 9001, "vlan_tag": 900, "mgmt_ip": "10.9.0.10"},
        {"restore_vmid": 9002, "helper_vmid": 9003, "vlan_tag": 901, "mgmt_ip": "10.9.0.11"},
        {"restore_vmid": 9004, "helper_vmid": 9005, "vlan_tag": 902, "mgmt_ip": "10.9.0.12"},
    ]
    assert allocate(owned=owned, **RANGES) is None


def test_returns_none_when_vmids_exhausted():
    owned = [{"restore_vmid": 9000, "helper_vmid": 9001, "vlan_tag": 900, "mgmt_ip": "10.9.0.10"}]
    tight = dict(RANGES, vmid_from=9000, vmid_to=9001)
    assert allocate(owned=owned, **tight) is None
