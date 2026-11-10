"""Pure tests for LXC-specific restore helpers (config parsing, net rewrite)."""

from app.services.restore_lxc import (
    bind_mount_deletes,
    lxc_config_nics,
    parse_lxc_net,
    rewrite_lxc_net_line,
)


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


def test_rewrite_keeps_hwaddr_and_ip_repoints_bridge_and_tag():
    v = "name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:DD:EE:FF,ip=10.0.0.5/24,gw=10.0.0.1,tag=7"
    out = rewrite_lxc_net_line(v, "vmbr9", 901)
    assert "bridge=vmbr9" in out and "tag=901" in out
    assert "hwaddr=AA:BB:CC:DD:EE:FF" in out and "ip=10.0.0.5/24" in out
    assert "bridge=vmbr0" not in out and "tag=7" not in out


def test_rewrite_adds_trunk_for_internally_tagged_guest():
    out = rewrite_lxc_net_line("name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:DD:EE:FF", "vmbr9", 901, trunks=1010)
    assert "trunks=1010" in out


def test_bind_mounts_are_deleted_volume_mounts_kept():
    cfg = {
        "mp0": "/srv/host-data,mp=/data",  # host bind-mount → delete
        "mp1": "local-lvm:vm-123-disk-1,mp=/var/lib/db",  # volume mount → keep
        "rootfs": "local-lvm:vm-123-disk-0,size=8G",
    }
    assert bind_mount_deletes(cfg) == ["mp0"]


def test_bind_mount_deletes_orders_by_index():
    cfg = {"mp2": "/a,mp=/a", "mp0": "/b,mp=/b", "mp1": "pool:disk,mp=/c"}
    assert bind_mount_deletes(cfg) == ["mp0", "mp2"]
