"""Pure-logic tests for guest IP collection."""

from app.services.restore_ip_collect import (
    collect_for_vm,
    macs_from_config,
    mark_primary,
    nics_from_agent,
    primary_ip,
)

AGENT = {
    "result": [
        {
            "name": "lo",
            "hardware-address": "00:00:00:00:00:00",
            "ip-addresses": [{"ip-address": "127.0.0.1", "ip-address-type": "ipv4", "prefix": 8}],
        },
        {
            "name": "eth0",
            "hardware-address": "AA:BB:CC:DD:EE:01",
            "ip-addresses": [
                {"ip-address": "10.0.0.50", "ip-address-type": "ipv4", "prefix": 24},
                {"ip-address": "fe80::1", "ip-address-type": "ipv6", "prefix": 64},
            ],
        },
    ]
}
CONFIG = {"net0": "virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0,tag=5", "name": "web01"}


def _pve_get(path):
    if path.endswith("/config"):
        return CONFIG
    if path.endswith("/network-get-interfaces"):
        return AGENT
    raise AssertionError(path)


def test_macs_from_config():
    assert macs_from_config(CONFIG) == {0: "aa:bb:cc:dd:ee:01"}


def test_nics_from_agent_filters_loopback_and_v6():
    nics = nics_from_agent(AGENT["result"])
    assert len(nics) == 1
    assert nics[0]["ip_addresses"] == ["10.0.0.50"]
    assert nics[0]["prefix"] == 24


def test_collect_assigns_nic_index_from_config_mac():
    rows = collect_for_vm({"node_name": "pve1", "vmid": 100, "guest_type": "qemu"}, _pve_get)
    assert rows == [
        {"nic_index": 0, "mac": "aa:bb:cc:dd:ee:01", "ip_addresses": ["10.0.0.50"], "prefix": 24, "source": "agent"}
    ]


def test_missing_agent_yields_no_ips():
    def boom(path):
        if path.endswith("/config"):
            return CONFIG
        raise RuntimeError("no agent")

    assert collect_for_vm({"node_name": "pve1", "vmid": 100, "guest_type": "qemu"}, boom) == []


def test_primary_is_first_non_loopback_v4_and_override_wins():
    nics = [{"nic_index": 1, "ip_addresses": ["10.0.0.9"]}, {"nic_index": 0, "ip_addresses": ["10.0.0.50"]}]
    assert primary_ip(nics) == "10.0.0.50"
    assert primary_ip(nics, override="10.0.0.9") == "10.0.0.9"


def test_mark_primary_sets_flag():
    nics = [{"nic_index": 0, "ip_addresses": ["10.0.0.50"]}, {"nic_index": 1, "ip_addresses": ["10.0.0.9"]}]
    marked = mark_primary(nics)
    assert marked[0]["is_primary"] is True
    assert marked[1]["is_primary"] is False
