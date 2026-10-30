"""Unit tests for the active restore-server client (pure helpers + orchestration
with injected fake PVE/SSH transports; no real host)."""

import pytest

from app.services.restore_server import (
    HostIdentityError,
    RestoreServer,
    RestoreTaskError,
    dnsmasq_config,
    parse_task_progress,
    rewrite_net_line,
    select_reaper_targets,
    stall_exceeded,
)

# --- pure helpers ---


def test_rewrite_net_line_preserves_mac_changes_bridge_and_tag():
    out = rewrite_net_line("virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0,tag=5", "vmbr9", 901)
    assert "virtio=AA:BB:CC:DD:EE:01" in out
    assert "bridge=vmbr9" in out
    assert "tag=901" in out
    assert "tag=5" not in out
    assert "vmbr0" not in out


def test_rewrite_net_line_adds_bridge_when_missing_and_no_tag():
    out = rewrite_net_line("virtio=AA:BB:CC:DD:EE:01", "vmbr9", None)
    assert out == "virtio=AA:BB:CC:DD:EE:01,bridge=vmbr9"


def test_select_reaper_targets():
    guests = [{"vmid": 100}, {"vmid": 9000}, {"vmid": 9001}]
    assert [g["vmid"] for g in select_reaper_targets(guests, {9000, 9001})] == [100]
    assert len(select_reaper_targets(guests, set())) == 3


def test_parse_task_progress():
    assert parse_task_progress("progress 42% (1.0 GiB of 10.0 GiB)") == {
        "percent": 42,
        "bytes_done": 1024**3,
        "bytes_total": 10 * 1024**3,
    }
    assert parse_task_progress("starting restore") is None


def test_stall_backstop_tolerates_slow_huge_restore():
    # 1% took 1h so far; a 50-min quiet gap is NOT a stall (factor 6 → ~6h allowed).
    assert stall_exceeded(50 * 60, percent_done=1, elapsed_seconds=3600, factor=6.0) is False
    # No progress for 10h at that rate IS a stall.
    assert stall_exceeded(10 * 3600, percent_done=1, elapsed_seconds=3600, factor=6.0) is True


def test_dnsmasq_config_has_reservation_and_range():
    conf = dnsmasq_config([{"mac": "aa:bb:cc:dd:ee:01", "ip": "10.0.0.50", "prefix": 24}], probe_offset=254)
    assert "dhcp-host=aa:bb:cc:dd:ee:01,10.0.0.50,infinite" in conf
    assert "dhcp-range=10.0.0.254,10.0.0.254,static,infinite" in conf


# --- orchestration with fakes ---


class FakePve:
    def __init__(self, responses):
        self.responses = responses
        self.posts = []
        self.deletes = []

    def get(self, path):
        # Longest matching prefix wins, so "/nodes/r1/qemu" beats "/nodes".
        best = None
        for key, val in self.responses.items():
            if path.startswith(key) and (best is None or len(key) > len(best[0])):
                best = (key, val)
        if best is None:
            return None
        key, val = best
        return val(path) if callable(val) else val

    def post(self, path, data):
        self.posts.append((path, data))
        return "UPID:restore:task"

    def delete(self, path):
        self.deletes.append(path)


class FakeSsh:
    def __init__(self):
        self.commands = []

    def run(self, command):
        self.commands.append(command)
        return ""


def _server(responses):
    cfg = {"ssh_host": "10.0.0.9", "api_url": "https://10.0.0.9:8006", "sandbox_bridge": "vmbr9"}
    return RestoreServer(cfg, FakePve(responses), FakeSsh())


def test_verify_host_identity_refuses_production():
    srv = _server({})
    with pytest.raises(HostIdentityError):
        srv.verify_host_identity({"10.0.0.9"})
    # a non-production host is fine
    srv.verify_host_identity({"10.1.2.3"})


def test_rewrite_nics_only_touches_net_lines_and_keeps_mac():
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/qemu/9000/config": {
            "net0": "virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0,tag=5",
            "name": "web",
            "scsi0": "local:vm-9000-disk-0",
        },
    }
    srv = _server(responses)
    srv.rewrite_nics(9000, 901)
    path, data = srv.pve.posts[-1]
    assert path == "/nodes/r1/qemu/9000/config"
    assert data == {"net0": "virtio=AA:BB:CC:DD:EE:01,bridge=vmbr9,tag=901"}


def test_restore_raises_on_task_error():
    responses = {
        "/nodes/r1/tasks": lambda p: {"status": "stopped", "exitstatus": "boom"} if p.endswith("status") else [],
        "/nodes": [{"node": "r1", "status": "online"}],
    }
    srv = _server(responses)
    with pytest.raises(RestoreTaskError):
        srv.restore(9000, {"storage": "pbs", "src_vmid": 100}, "snap", "zfs", sleep=lambda s: None)


def test_restore_completes_on_ok_status():
    state = {"calls": 0}

    def status(path):
        state["calls"] += 1
        return {"status": "running"} if state["calls"] < 2 else {"status": "stopped", "exitstatus": "OK"}

    responses = {
        "/nodes/r1/tasks": lambda p: status(p) if p.endswith("status") else [{"t": "progress 100% (1B of 1B)"}],
        "/nodes": [{"node": "r1", "status": "online"}],
    }
    srv = _server(responses)
    seen = []
    srv.restore(9000, {"storage": "pbs", "src_vmid": 100}, "snap", "zfs", on_progress=seen.append, sleep=lambda s: None)
    assert any(p.get("percent") == 100 for p in seen)


def test_reap_destroys_unowned():
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/qemu": [{"vmid": 100}, {"vmid": 9000}],
        "/nodes/r1/lxc": [{"vmid": 9001}],
    }
    srv = _server(responses)
    destroyed = srv.reap({9000, 9001})
    assert destroyed == [100]
    assert srv.pve.deletes == ["/nodes/r1/qemu/100?purge=1&destroy-unreferenced-disks=1"]
