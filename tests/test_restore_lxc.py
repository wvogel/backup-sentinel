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


def test_is_supported_guest():
    from app.services.restore_orchestrator import is_supported_guest

    assert is_supported_guest("qemu") and is_supported_guest("lxc")
    assert is_supported_guest(None)  # absent guest_type defaults to qemu
    assert not is_supported_guest("other")


def _lxc_plan(**over):
    from app.services.restore_worker import RunPlan

    base = dict(
        vm_id=1,
        restore_vmid=9000,
        helper_vmid=9001,
        vlan_tag=901,
        mgmt_ip="10.0.0.10",
        source={},
        snapshot_id="s",
        target_storage="ct-store",
        reservations=[],
        probe_targets=[],
        primary_ip="10.0.0.50",
        tcp_ports=[443],
        guest_type="lxc",
    )
    base.update(over)
    return RunPlan(**base)


class _TeardownFake:
    """Models the qemu/lxc endpoint split: a guest exists only under its own kind."""

    def __init__(self, guest_is_lxc):
        self.guest_is_lxc = guest_is_lxc
        self.destroyed = []
        self.diag_nic_calls = []

    def exists(self, vmid, is_lxc=False):
        if vmid == 9001:  # the helper is always an LXC
            return is_lxc
        return is_lxc == self.guest_is_lxc

    def destroy(self, vmid, is_lxc=False):
        self.destroyed.append((vmid, is_lxc))

    def reap(self, owned):
        pass

    def guest_agent_nics(self, vmid):
        self.diag_nic_calls.append("guest_agent_nics")
        return []

    def lxc_nics(self, vmid):
        self.diag_nic_calls.append("lxc_nics")
        return [("172.30.10.9", 24)]

    def probe_netdiag(self, *a, **k):
        return ""

    def mgmt_netdiag(self, *a, **k):
        return ""


def test_teardown_destroys_restored_container_as_lxc(monkeypatch):
    from app.services import restore_worker

    monkeypatch.setattr(restore_worker, "owned_vmids", lambda: set())
    srv = _TeardownFake(guest_is_lxc=True)
    restore_worker._teardown(srv, _lxc_plan())
    assert (9000, True) in srv.destroyed  # the container is destroyed via the lxc endpoint
    assert (9001, True) in srv.destroyed  # and the helper (always lxc)


def test_failure_diag_uses_lxc_nics_for_a_container(monkeypatch):
    from app.services import restore_worker

    srv = _TeardownFake(guest_is_lxc=True)
    restore_worker._failure_diag(srv, _lxc_plan())
    assert srv.diag_nic_calls == ["lxc_nics"]  # a container has no guest agent


def test_runplan_carries_guest_type():
    from app.services.restore_worker import RunPlan

    base = dict(
        vm_id=1,
        restore_vmid=9000,
        helper_vmid=9001,
        vlan_tag=901,
        mgmt_ip="10.0.0.10",
        source={},
        snapshot_id="s",
        target_storage="x",
        reservations=[],
        probe_targets=[],
        primary_ip=None,
    )
    assert RunPlan(**base).guest_type == "qemu"
    assert RunPlan(**base, guest_type="lxc").guest_type == "lxc"
