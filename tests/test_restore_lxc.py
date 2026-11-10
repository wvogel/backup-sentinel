"""Pure tests for LXC-specific restore helpers (config parsing, net rewrite)."""

from app.services.restore_lxc import lxc_config_nics, parse_lxc_net


def test_parse_static_cidr():
    v = "name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:DD:EE:FF,ip=10.0.0.5/24,gw=10.0.0.1,tag=5"
    assert parse_lxc_net(v) == {"mac": "aa:bb:cc:dd:ee:ff", "ip": "10.0.0.5", "prefix": 24}


def test_parse_dhcp_has_no_ip():
    v = "name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:DD:EE:FF,ip=dhcp"
    assert parse_lxc_net(v) == {"mac": "aa:bb:cc:dd:ee:ff", "ip": None, "prefix": None}


def test_parse_missing_ip():
    v = "name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:DD:EE:FF"
    assert parse_lxc_net(v) == {"mac": "aa:bb:cc:dd:ee:ff", "ip": None, "prefix": None}


def test_config_nics_orders_and_skips_non_static():
    cfg = {
        "net1": "name=eth1,hwaddr=BB:BB:BB:BB:BB:BB,ip=dhcp",
        "net0": "name=eth0,hwaddr=AA:AA:AA:AA:AA:AA,ip=192.168.1.9/26,gw=192.168.1.1",
        "rootfs": "local:vm-123-disk-0,size=8G",
    }
    assert lxc_config_nics(cfg) == [("192.168.1.9", 26)]
