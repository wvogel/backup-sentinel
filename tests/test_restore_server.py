"""Unit tests for the active restore-server client (pure helpers + orchestration
with injected fake PVE/SSH transports; no real host)."""

import pytest

from app.services.restore_server import (
    DISK_CLASS_MAX_BPS,
    HostIdentityError,
    RestoreServer,
    RestoreTaskError,
    _parse_diskstats,
    _parse_netdev,
    _parse_speeds,
    _stable_mac,
    backup_error_hint,
    classify_task_error,
    compute_io_rates,
    dnsmasq_config,
    eject_cdroms,
    helper_legs,
    parse_task_progress,
    rewrite_net_line,
    sandbox_addrs,
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
    assert out == "virtio=AA:BB:CC:DD:EE:01,bridge=vmbr9,mtu=1500"


def test_rewrite_net_line_pins_mtu_1500():
    # A jumbo-frame source NIC (mtu=9000) would not start on the 1500 sandbox
    # bridge; VirtIO gets mtu=1500, other models no mtu (they use the bridge's).
    out = rewrite_net_line("virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0,mtu=9000", "vmbr9", 901)
    assert "mtu=9000" not in out and out.count("mtu=") == 1 and "mtu=1500" in out
    out = rewrite_net_line("e1000=AA:BB:CC:DD:EE:01,bridge=vmbr0,mtu=9000", "vmbr9", 901)
    assert "mtu=" not in out


def test_sandbox_name_builds_hostname_from_cluster_and_vm():
    from app.services.restore_server import sandbox_name

    assert sandbox_name("Prod", "pbs-fh-muc") == "prod-pbs-fh-muc"
    assert sandbox_name("Prod MUC", "web01.example") == "prod-muc-web01-example"
    assert sandbox_name("Prod", "pbs-fh-muc", suffix="helper") == "prod-pbs-fh-muc-helper"
    assert sandbox_name("", "") == "restore"  # never empty
    long = sandbox_name("c" * 80, "v" * 80, suffix="helper")
    assert len(long) <= 63 and long.endswith("-helper")  # suffix always preserved


def test_rewrite_net_line_drops_firewall_keeps_other_opts():
    # firewall=1 puts the guest behind the Proxmox NIC firewall, which drops the
    # probe's ICMP/TCP — must be stripped. Other opts (queues) are preserved.
    out = rewrite_net_line("virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0,firewall=1,queues=4,tag=5", "vmbr9", 901)
    assert "firewall=1" not in out and "firewall=" not in out
    assert "queues=4" in out
    assert "bridge=vmbr9" in out and "tag=901" in out


def test_classify_task_error_wrong_key():
    tail = "Error: unable to load encryption key - decryption failed\nTASK ERROR"
    msg = classify_task_error("command '/usr/bin/proxmox-backup-client restore ...' failed: exit code 255", tail)
    assert "decryption failed" in msg and "key" in msg.lower()


def test_classify_task_error_no_false_positive_on_crypt_mode():
    # The command line always carries --crypt-mode=encrypt for encrypted backups;
    # a failure whose tail is a PERMISSION error must NOT be reported as a key
    # mismatch just because the command mentions encryption.
    exitstatus = "command '/usr/bin/proxmox-backup-client restore '--crypt-mode=encrypt' ...' failed: exit code 255"
    tail = "Error: permission check failed - missing Datastore.Read on /datastore/nas-off/ns/fh-muc"
    msg = classify_task_error(exitstatus, tail)
    assert "permission denied" in msg
    assert "decryption" not in msg.lower()


def test_classify_task_error_falls_back_to_detail():
    msg = classify_task_error("exit code 255", "something unrecognised went wrong")
    assert msg.startswith("restore task failed:")
    assert "unrecognised" in msg


def test_parse_fio_bw_reads_write_bandwidth():
    from app.services.restore_server import parse_fio_bw

    assert parse_fio_bw('{"jobs":[{"write":{"bw_bytes":524288000}}]}') == 524288000
    # fio prints a banner before the JSON — scan to the outer braces.
    assert parse_fio_bw('fio-3.33\nStarting 1 process\n{"jobs":[{"write":{"bw_bytes":100}}]}\n') == 100
    assert parse_fio_bw("not json at all") is None
    assert parse_fio_bw('{"jobs":[{"read":{"bw_bytes":5}}]}') is None  # no write result


def test_disk_ceiling_prefers_measured_over_class():
    from app.services.restore_server import DISK_CLASS_MAX_BPS

    srv = _server({})
    classes = {"fast": "enterprise-ssd", "slow": "sata-hdd"}
    # A measured value for 'slow' wins (it is the max here).
    assert srv._disk_ceiling_bps(classes, {"fast", "slow"}, {"slow": 9_000_000_000}) == 9_000_000_000
    # No measurement → the fastest disk-class estimate among the selected.
    assert srv._disk_ceiling_bps(classes, {"fast", "slow"}, {}) == DISK_CLASS_MAX_BPS["enterprise-ssd"]
    # A measured value is per-storage: 'fast' measured low, 'slow' by class.
    assert srv._disk_ceiling_bps(classes, {"fast", "slow"}, {"fast": 1_000}) == DISK_CLASS_MAX_BPS["sata-hdd"]


def test_backup_error_hint_recognises_and_ignores():
    assert "decryption failed" in backup_error_hint("Error: wrong key - unable to verify signature")
    assert "permission denied" in backup_error_hint("permission check failed - missing Datastore.Read")
    assert backup_error_hint("some totally unrelated blip") == ""


def test_preflight_passes_when_config_extracts():
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/vzdump/extractconfig": "boot: order=scsi0\nnet0: virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0",
    }
    # No raise, and it asked for the right volid.
    _server(responses).preflight_check({"storage": "pbs-x", "src_vmid": 100}, "2026-01-01T00:00:00Z")


def test_preflight_aborts_on_wrong_key():
    def boom(_path):
        raise RuntimeError(
            "PVE-Restore API returned HTTP 500 for /nodes/r1/vzdump/extractconfig: "
            "Could not extract backed up config: wrong key - unable to verify signature"
        )

    responses = {"/nodes": [{"node": "r1", "status": "online"}], "/nodes/r1/vzdump/extractconfig": boom}
    with pytest.raises(RestoreTaskError) as ei:
        _server(responses).preflight_check({"storage": "pbs-x", "src_vmid": 100}, "snap")
    assert "decryption failed" in str(ei.value).lower()


def test_preflight_proceeds_on_inconclusive_error():
    # An unrecognised failure (e.g. endpoint absent on an older PVE) must NOT
    # abort a restore that might well succeed.
    def boom(_path):
        raise RuntimeError("endpoint not implemented")

    responses = {"/nodes": [{"node": "r1", "status": "online"}], "/nodes/r1/vzdump/extractconfig": boom}
    _server(responses).preflight_check({"storage": "pbs-x", "src_vmid": 100}, "snap")  # no raise


def test_pvesh_command_maps_rest_to_cli():
    from app.services.restore_server import pvesh_command, split_path_query

    path, params = split_path_query("/nodes/r1/storage/local/content?content=vztmpl&vmid=100")
    assert path == "/nodes/r1/storage/local/content"
    assert params == {"content": "vztmpl", "vmid": "100"}
    cmd = pvesh_command("get", path, params)
    assert cmd == "pvesh get /nodes/r1/storage/local/content -content vztmpl -vmid 100 --output-format json"
    # A net config line is passed as a single option value (its =,:.- chars are
    # shell-safe, so shlex leaves it unquoted — still one argument to pvesh).
    cmd2 = pvesh_command("set", "/nodes/r1/qemu/9000/config", {"net0": "name=sbx,bridge=vmbr9,tag=901"})
    assert "pvesh set /nodes/r1/qemu/9000/config -net0 name=sbx,bridge=vmbr9,tag=901 " in cmd2


class _RecSsh:
    """Records pvesh commands and returns canned JSON per command substring."""

    def __init__(self, replies=None):
        self.commands = []
        self.puts = []
        self.replies = replies or {}

    def run(self, command):
        self.commands.append(command)
        for needle, reply in self.replies.items():
            if needle in command:
                return reply
        return ""

    def put_bytes(self, remote_path, data):
        self.puts.append((remote_path, len(data)))


def test_pve_ssh_post_uses_set_for_config_create_otherwise():
    from app.services.restore_server import PveSsh

    ssh = _RecSsh()
    pve = PveSsh(ssh)
    pve.post("/nodes/r1/qemu/9000/config", {"net0": "x"})  # config → set (PUT)
    pve.post("/nodes/r1/qemu/9000/status/start", {})  # not config → create (POST)
    assert ssh.commands[0].startswith("pvesh set /nodes/r1/qemu/9000/config")
    assert ssh.commands[1].startswith("pvesh create /nodes/r1/qemu/9000/status/start")


def test_pve_ssh_upload_sfts_into_template_cache():
    from app.services.restore_server import PveSsh

    ssh = _RecSsh(replies={"pvesh get /storage/local": '{"path": "/var/lib/vz"}'})
    pve = PveSsh(ssh)
    assert pve.upload("r1", "local", "restore-helper-abc.tar.gz", b"ROOTFS") is None
    assert ssh.puts == [("/var/lib/vz/template/cache/restore-helper-abc.tar.gz", 6)]
    assert any("mkdir -p /var/lib/vz/template/cache" in c for c in ssh.commands)


def test_rewrite_nics_strips_hostpci_before_start():
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/qemu/9000/config": {
            "net0": "virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0,tag=5",
            "hostpci0": "0000:01:00.0,pcie=1",
            "hostpci1": "0000:02:00.0",
            "scsi0": "local:vm-9000-disk-0",
        },
    }
    srv = _server(responses)
    srv.rewrite_nics(9000, 901)
    # NIC/CD-ROM go via the API token; the hostpci delete is a separate root call
    # (pve_root defaults to the same fake here, so both land in srv.pve.posts).
    cfg_posts = [d for p, d in srv.pve.posts if p == "/nodes/r1/qemu/9000/config"]
    nic = next(d for d in cfg_posts if "net0" in d)
    assert nic["net0"] == "virtio=AA:BB:CC:DD:EE:01,bridge=vmbr9,mtu=1500,tag=901"
    assert "delete" not in nic  # hostpci not deleted in the token-safe update
    delete = next(d for d in cfg_posts if "delete" in d)
    assert delete["delete"] == "hostpci0,hostpci1"  # both passthrough devices removed via root


def test_eject_cdroms_empties_source_isos_only():
    config = {
        "ide2": "nas-pve-aboe-muc:iso/virtio.iso,media=cdrom",  # source ISO → eject
        "ide0": "local-lvm:vm-9000-cloudinit,media=cdrom",  # cloudinit cdrom → eject
        "sata0": "none,media=cdrom",  # already empty → leave
        "scsi0": "local:vm-9000-disk-0,size=32G",  # real disk → leave
        "net0": "virtio=AA:BB,bridge=vmbr0",  # not a drive → leave
    }
    assert eject_cdroms(config) == {
        "ide2": "none,media=cdrom",
        "ide0": "none,media=cdrom",
    }


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


def test_parse_task_progress_proxmox_read_bytes_format():
    # Proxmox's own line: percent plus "read N bytes" (no total).
    out = parse_task_progress(
        "progress 90% (read 30924603392 bytes, zeroes = 60% (18668847104 bytes), duration 39 sec)"
    )
    assert out == {"percent": 90, "bytes_done": 30924603392}


def test_stall_backstop_tolerates_slow_huge_restore():
    # 1% took 1h so far; a 50-min quiet gap is NOT a stall (factor 6 → ~6h allowed).
    assert stall_exceeded(50 * 60, percent_done=1, elapsed_seconds=3600, factor=6.0) is False
    # No progress for 10h at that rate IS a stall.
    assert stall_exceeded(10 * 3600, percent_done=1, elapsed_seconds=3600, factor=6.0) is True


def test_stable_mac_is_deterministic_and_ip_derived():
    assert _stable_mac("10.20.0.10") == "02:00:0a:14:00:0a"
    assert _stable_mac("10.20.0.10") == _stable_mac("10.20.0.10")  # stable across calls
    assert _stable_mac("10.121.101.100") != _stable_mac("10.121.101.101")  # unique per IP


def test_sandbox_addrs_fixed_hosts_in_mgmt_subnet():
    # Fixed sandbox addresses derived from the management subnet, independent of
    # the restored guest: helper .254, guest .200, plus the netmask.
    helper_ip, guest_ip, netmask = sandbox_addrs("10.121.101.100", 24)
    assert helper_ip == "10.121.101.254"
    assert guest_ip == "10.121.101.200"
    assert netmask == "255.255.255.0"


def test_sandbox_fallback_addrs_are_isolated_from_mgmt():
    from app.services.restore_server import sandbox_fallback_addrs

    # The DHCP fallback lives in the dedicated isolated range, not the mgmt subnet.
    helper_ip, guest_ip, prefix = sandbox_fallback_addrs()
    assert helper_ip == "100.64.0.254"
    assert guest_ip == "100.64.0.200"
    assert prefix == 24


def test_helper_legs_dual_homed():
    # net0 (sbx) carries the helper's own address in the GUEST's subnet, with the
    # guest's prefix (not always /24) — here a /26.
    nets = helper_legs("vmbr9", 901, "vmbr0", "10.20.0.10", 24, None, "", "10.0.5.1", 26)
    assert "net1" in nets  # separate management leg
    assert "name=sbx" in nets["net0"]
    assert "tag=901" in nets["net0"] and "10.0.5.1/26" in nets["net0"]
    assert "10.20.0.10/24" in nets["net1"]
    # Stable IP-derived MAC on the mgmt leg (keeps the gateway's ARP entry valid).
    assert "hwaddr=02:00:0a:14:00:0a" in nets["net1"]


def test_helper_legs_mgmt_leg_carries_gateway_and_vlan():
    # The management leg must stay on the real interface (vmbr0) with the mgmt IP,
    # gateway and VLAN tag so the app can reach the agent; the sandbox leg (net0)
    # stays on the isolated bridge with the guest-subnet sandbox IP. Here the guest
    # subnet IS the management subnet (10.20.0.0/24), so the sandbox leg is pinned
    # to /32 — two connected /24s on one helper would make the mgmt leg unreachable.
    nets = helper_legs("vmbr9", 901, "vmbr0", "10.20.0.10", 24, 1010, "10.20.0.1", "10.20.0.254", 24)
    assert "bridge=vmbr0" in nets["net1"] and "10.20.0.10/24" in nets["net1"]
    assert "gw=10.20.0.1" in nets["net1"] and "tag=1010" in nets["net1"]
    assert "bridge=vmbr9" in nets["net0"] and "10.20.0.254/32" in nets["net0"]


def test_helper_legs_sandbox_prefix_kept_when_distinct_subnet():
    # Guest subnet differs from the mgmt subnet → no collision, keep the guest's
    # own prefix on the sandbox leg (here /26); pin to /32 only on an overlap.
    distinct = helper_legs("vmbr9", 901, "vmbr0", "10.20.0.10", 24, None, "", "10.0.5.1", 26)
    assert "10.0.5.1/26" in distinct["net0"]
    overlap = helper_legs("vmbr9", 901, "vmbr0", "172.30.44.110", 24, 2040, "172.30.44.254", "172.30.44.60", 24)
    assert "172.30.44.60/32" in overlap["net0"]  # same /24 as mgmt → /32


def test_dnsmasq_config_single_mgmt_range_ip_on_sandbox_leg():
    guest_ip, netmask = "10.0.0.200", "255.255.255.0"
    conf = dnsmasq_config(guest_ip, netmask)
    # Bound to the sandbox leg only, never the real management network.
    assert "interface=sbx" in conf
    assert "bind-interfaces" in conf
    # One-address dynamic range: whatever NIC asks first gets the guest IP.
    assert "dhcp-range=10.0.0.200,10.0.0.200,255.255.255.0,infinite" in conf
    # Must bind ONLY the sandbox leg, never the management leg — otherwise a
    # guest subnet that overlaps the mgmt subnet turns the helper into a rogue
    # DHCP server on the real management network.
    assert "interface=sbx" in conf


# --- orchestration with fakes ---


class FakePve:
    def __init__(self, responses):
        self.responses = responses
        self.posts = []
        self.deletes = []
        self.uploads = []

    def get(self, path, timeout=None):
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

    def upload(self, node, storage, filename, data):
        self.uploads.append((node, storage, filename, len(data)))
        return ""  # no task UPID → no wait in the test


class FakeSsh:
    def __init__(self):
        self.commands = []
        self.puts = []

    def run(self, command):
        self.commands.append(command)
        return ""

    def put_bytes(self, remote_path, data):
        self.puts.append((remote_path, len(data)))


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
    assert data == {"net0": "virtio=AA:BB:CC:DD:EE:01,bridge=vmbr9,mtu=1500,tag=901"}


def test_restore_raises_on_task_error():
    responses = {
        "/nodes/r1/tasks": lambda p: {"status": "stopped", "exitstatus": "boom"} if p.endswith("status") else [],
        "/nodes": [{"node": "r1", "status": "online"}],
    }
    srv = _server(responses)
    srv._start_restore_task = lambda *a, **k: "UPID:restore:task"  # don't shell out in the test
    with pytest.raises(RestoreTaskError):
        srv.restore(9000, {"storage": "pbs", "src_vmid": 100}, "snap", "zfs", sleep=lambda s: None)


def test_wait_unlocked_returns_when_no_lock():
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/qemu/9000/config": {"name": "web"},  # no 'lock'
    }
    _server(responses).wait_unlocked(9000, attempts=3, sleep=lambda s: None)  # returns, no raise


def test_wait_unlocked_raises_when_still_locked():
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/qemu/9000/config": {"lock": "create"},  # restore still holds the lock
    }
    with pytest.raises(RestoreTaskError, match="locked"):
        _server(responses).wait_unlocked(9000, attempts=2, sleep=lambda s: None)


def test_restore_cancel_stops_task_and_raises():
    from app.services.restore_server import RestoreCancelled

    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/tasks": lambda p: {"status": "running"} if p.endswith("status") else [],
    }
    srv = _server(responses)
    srv._start_restore_task = lambda *a, **k: "UPID:restore:task"
    with pytest.raises(RestoreCancelled):
        srv.restore(
            9000, {"storage": "pbs", "src_vmid": 100}, "snap", "zfs", sleep=lambda s: None, should_cancel=lambda: True
        )
    assert any("/tasks/" in d for d in srv.pve.deletes)  # stop_task issued a task DELETE


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
    srv._start_restore_task = lambda *a, **k: "UPID:restore:task"
    seen = []
    srv.restore(9000, {"storage": "pbs", "src_vmid": 100}, "snap", "zfs", on_progress=seen.append, sleep=lambda s: None)
    assert any(p.get("percent") == 100 for p in seen)


def test_start_restore_task_discovers_running_upid_not_a_finished_one():
    running = "UPID:r1:0000ABCD:00002000:qmrestore:9000:root@pam:"
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/tasks": [
            # A FINISHED restore of the same (reused) vmid must be ignored, else we
            # poll an already-stopped task and jump to "booting/100%".
            {
                "upid": "UPID:r1:OLD:00001000:qmrestore:9000:root@pam:",
                "id": "9000",
                "type": "qmrestore",
                "endtime": 111,
                "status": "OK",
                "starttime": 1000,
            },
            {"upid": running, "id": "9000", "type": "qmrestore", "starttime": 2000},  # running (no endtime)
        ],
    }
    srv = _server(responses)
    got = srv._start_restore_task(9000, "store:backup/vm/100/x", "zfs", "r1", sleep=lambda s: None)
    assert got == running
    assert any("pvesh create /nodes/r1/qemu" in c and "nohup" in c for c in srv.ssh.commands)


def test_provision_helper_rootfs_on_first_container_capable_target():
    # The sorted-first target (restore-vms) is images-only, so the helper skips it
    # and lands on the next selected one that accepts containers (rootdir).
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/storage": [
            {"storage": "restore-vms", "content": "images"},
            {"storage": "restore-ct", "content": "images,rootdir"},
        ],
    }
    cfg = {
        "ssh_host": "10.0.0.9",
        "api_url": "https://10.0.0.9:8006",
        "sandbox_bridge": "vmbr9",
        "mgmt_iface": "vmbr0",
        "mgmt_netmask": "255.255.255.0",
        "helper_template": "local:vztmpl/alpine.tar.zst",
        "probe_offset": 254,
        "target_storages": [
            {"storage": "restore-vms", "use": True, "order": 0},
            {"storage": "restore-ct", "use": True, "order": 1},
        ],
    }
    srv = RestoreServer(cfg, FakePve(responses), FakeSsh())
    posted = []
    srv.ensure_helper_template = lambda: "local:vztmpl/restore-helper.tar.gz"
    srv._agent_wait_healthy = lambda ip, **k: None  # no real HTTP in the test
    srv._agent_post = lambda ip, path, body, **k: posted.append((ip, path, body)) or {}
    srv.provision_helper(9001, 901, "10.20.0.10", "10.0.0.1", "10.0.0.50", 24)
    # Container created via the API token; rootfs on the first container-capable
    # selected target (images-only "restore-vms" skipped); dnsmasq config pushed
    # to the agent over the management IP (no SSH).
    create = [d for p, d in srv.pve.posts if p == "/nodes/r1/lxc"]
    assert create, srv.pve.posts
    assert create[0]["storage"] == "restore-ct"
    assert create[0]["ostemplate"] == "local:vztmpl/restore-helper.tar.gz"
    assert any(ip == "10.20.0.10" and path == "/dnsmasq" for ip, path, _ in posted)
    assert not any(c.startswith("pct ") for c in srv.ssh.commands)  # no pct exec


def test_provision_helper_sandbox_leg_on_guest_vlan_when_guest_tags():
    # Guest tags internally (guest_vlan set): the helper's sandbox leg becomes an
    # access port on THAT VID (not the env's 9xx) so PVE delivers the guest's
    # tagged frames to the helper untagged — no in-container sub-interface needed.
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/storage": [{"storage": "restore-ct", "content": "rootdir"}],
    }
    cfg = {
        "ssh_host": "10.0.0.9",
        "api_url": "https://10.0.0.9:8006",
        "sandbox_bridge": "vmbr9",
        "mgmt_iface": "vmbr0",
        "mgmt_netmask": "255.255.255.0",
        "helper_template": "local:vztmpl/alpine.tar.zst",
        "target_storages": [{"storage": "restore-ct", "use": True, "order": 0}],
    }
    srv = RestoreServer(cfg, FakePve(responses), FakeSsh())
    srv.ensure_helper_template = lambda: "local:vztmpl/restore-helper.tar.gz"
    srv._agent_wait_healthy = lambda ip, **k: None
    srv._agent_post = lambda ip, path, body, **k: {}
    srv.provision_helper(9001, 901, "10.20.0.10", "10.0.5.1", "10.0.5.3", 26, 20)
    create = next(d for p, d in srv.pve.posts if p == "/nodes/r1/lxc")
    assert "tag=20" in create["net0"] and "tag=901" not in create["net0"]
    assert "10.0.5.1/26" in create["net0"]


def test_rewrite_nics_trunks_guest_vlan():
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/qemu/9000/config": {"net0": "virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0,tag=5"},
    }
    cfg = {"ssh_host": "10.0.0.9", "api_url": "https://10.0.0.9:8006", "sandbox_bridge": "vmbr9"}
    srv = RestoreServer(cfg, FakePve(responses), FakeSsh())
    srv.rewrite_nics(9000, 901, guest_vlan=20)
    changed = next(d for p, d in srv.pve.posts if p == "/nodes/r1/qemu/9000/config")
    assert "tag=901" in changed["net0"] and "trunks=20" in changed["net0"]


def test_rewrite_nics_renames_the_restored_vm():
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/qemu/9000/config": {"net0": "virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0,tag=5", "name": "pbs-fh-muc"},
    }
    cfg = {"ssh_host": "10.0.0.9", "api_url": "https://10.0.0.9:8006", "sandbox_bridge": "vmbr9"}
    srv = RestoreServer(cfg, FakePve(responses), FakeSsh())
    srv.rewrite_nics(9000, 901, name="prod-pbs-fh-muc")
    changed = next(d for p, d in srv.pve.posts if p == "/nodes/r1/qemu/9000/config")
    assert changed["name"] == "prod-pbs-fh-muc"  # cluster+VM, not the backup's bare name


def test_provision_helper_names_helper_after_cluster_vm():
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/storage": [{"storage": "restore-ct", "content": "rootdir"}],
    }
    cfg = {
        "ssh_host": "10.0.0.9",
        "api_url": "https://10.0.0.9:8006",
        "sandbox_bridge": "vmbr9",
        "mgmt_iface": "vmbr0",
        "mgmt_netmask": "255.255.255.0",
        "target_storages": [{"storage": "restore-ct", "use": True, "order": 0}],
    }
    srv = RestoreServer(cfg, FakePve(responses), FakeSsh())
    srv.ensure_helper_template = lambda: "local:vztmpl/restore-helper.tar.gz"
    srv._agent_wait_healthy = lambda ip, **k: None
    srv._agent_post = lambda ip, path, body, **k: {}
    srv.provision_helper(9001, 901, "10.20.0.10", "10.0.0.1", "10.0.0.50", 24, None, "prod-pbs-fh-muc")
    create = next(d for p, d in srv.pve.posts if p == "/nodes/r1/lxc")
    assert create["hostname"] == "prod-pbs-fh-muc-helper"


def test_probe_calls_the_agent_over_http():
    srv = _server({})
    srv._agent_post = lambda ip, path, body, **k: {"ip": ip, "path": path, "body": body}
    out = srv.probe("10.9.0.10", [{"ip": "10.0.0.50", "ports": [443]}], require_icmp=True)
    assert out["ip"] == "10.9.0.10" and out["path"] == "/probe"
    assert out["body"]["require_icmp"] is True


def test_helper_agent_probe_targets(monkeypatch):
    from app.services import restore_helper_agent as agent

    monkeypatch.setattr(agent, "_ping", lambda ip, timeout=2.0: ip == "10.0.0.50")
    monkeypatch.setattr(agent, "_tcp", lambda ip, port, timeout=2.0: port == 443)
    monkeypatch.setattr(agent, "_arp_alive", lambda ip: True)
    monkeypatch.setattr(agent, "_ensure_reachable", lambda ip, prefix=24: None)
    monkeypatch.setattr(agent, "_route_via_sbx", lambda ip: True)  # route is via the sandbox leg
    out = agent.probe_targets([{"ip": "10.0.0.50", "ports": [443, 22]}], require_icmp=True)
    assert out["10.0.0.50"]["icmp"] is True
    assert out["10.0.0.50"]["arp"] is True  # L2 liveness reported for the classifier
    # Key must be "ports" (the classifier reads primary["ports"]), not "tcp".
    assert out["10.0.0.50"]["ports"] == {"443": True, "22": False}


def test_helper_agent_probe_runs_checks_concurrently(monkeypatch):
    # A Docker-host guest makes the probe hit several targets/ports; they must run
    # together, not serialise (one closed port's 2s timeout behind another). Prove
    # it with a barrier: every check blocks until all are in flight — if any ran
    # serially the barrier would never fill and .result() would raise.
    import threading

    from app.services import restore_helper_agent as agent

    barrier = threading.Barrier(5)  # icmp + arp + 3 ports must be concurrent

    def waiter(*_a, **_k):
        barrier.wait(timeout=5)
        return True

    monkeypatch.setattr(agent, "_ping", lambda ip, timeout=2.0: waiter())
    monkeypatch.setattr(agent, "_arp_alive", lambda ip: waiter())
    monkeypatch.setattr(agent, "_tcp", lambda ip, port, timeout=2.0: waiter())
    monkeypatch.setattr(agent, "_ensure_reachable", lambda ip, prefix=24: None)
    monkeypatch.setattr(agent, "_route_via_sbx", lambda ip: True)
    out = agent.probe_targets([{"ip": "10.0.0.50", "ports": [443, 22, 8080]}], require_icmp=True)
    assert out["10.0.0.50"]["icmp"] is True
    assert out["10.0.0.50"]["arp"] is True
    assert out["10.0.0.50"]["ports"] == {"443": True, "22": True, "8080": True}


def test_helper_agent_ensure_reachable_pins_route_source(monkeypatch):
    from app.services import restore_helper_agent as agent

    calls = []
    monkeypatch.setattr(agent.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    agent._ensure_reachable("10.254.10.6", 24)
    # Adds a host in the guest's subnet as a /32 (never /prefix — a second connected
    # /24 would make the mgmt leg unreachable when both share the subnet) and a /32
    # route pinned to that source, so the probe leaves from the sandbox leg only.
    assert ["ip", "addr", "add", "10.254.10.1/32", "dev", "sbx"] in calls
    route = next(c for c in calls if c[:3] == ["ip", "route", "replace"])
    assert route == ["ip", "route", "replace", "10.254.10.6/32", "dev", "sbx", "src", "10.254.10.1"]


def test_helper_agent_arp_alive_reads_neighbour_table(monkeypatch):
    from app.services import restore_helper_agent as agent

    calls = {}

    class R:
        def __init__(self, stdout=""):
            self.stdout = stdout

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        if cmd[:2] == ["ip", "neigh"]:
            return R("10.254.10.10 dev sbx lladdr 52:54:00:aa:bb:cc REACHABLE\n")
        return R("")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    assert agent._arp_alive("10.254.10.10") is True

    def fake_run_incomplete(cmd, **kw):
        if cmd[:2] == ["ip", "neigh"]:
            return R("10.254.10.10 dev sbx  INCOMPLETE\n")
        return R("")

    monkeypatch.setattr(agent.subprocess, "run", fake_run_incomplete)
    assert agent._arp_alive("10.254.10.10") is False


def test_probe_netdiag_captures_sbx_arp_and_ping_state():
    srv = _server({"/nodes": [{"node": "r1", "status": "online"}]})

    def fake_run(cmd):
        if "ip neigh" in cmd:
            return "10.254.10.6 dev sbx lladdr 52:54:00:aa:bb:cc REACHABLE"
        if "addr show dev sbx" in cmd:
            return "2: sbx    inet 10.254.10.1/24 scope global sbx"
        if "ping" in cmd:
            raise RuntimeError("1 packets transmitted, 0 received")  # ICMP filtered
        return ""

    srv.ssh.run = fake_run
    out = srv.probe_netdiag(9001, ["10.254.10.6"])
    # Shows the sandbox leg's address, the resolved neighbour (L2 OK), and that
    # ping failed → the guest firewalls ICMP but is on the segment.
    assert "10.254.10.1/24" in out
    assert "Guest 10.254.10.6" in out
    assert "52:54:00:aa:bb:cc  REACHABLE" in out
    assert "ICMP       no answer" in out


def test_probe_netdiag_empty_without_helper():
    srv = _server({})
    assert srv.probe_netdiag(0, ["10.0.0.1"]) == ""


def test_mgmt_netdiag_probes_agent_inside_and_host_path():
    # The "helper agent unreachable: timed out" failure needs to tell apart: agent
    # down INSIDE the helper, L2/ARP from the host, IP-not-applied, or port blocked.
    srv = _server({})
    cmds: list[str] = []
    srv.ssh.run = lambda cmd: (cmds.append(cmd), "ok")[1]
    out = srv.mgmt_netdiag(9001, "10.121.101.100")
    joined = "\n".join(cmds)
    assert "ping -c1 -W2 10.121.101.100" in joined  # host → helper at L3
    assert "ip neigh show to 10.121.101.100" in joined  # host-side ARP state
    assert any("nc -z" in c and "10.121.101.100" in c for c in cmds)  # host → agent port
    assert any("pct exec 9001 -- nc -z" in c and "127.0.0.1" in c for c in cmds)  # agent listening INSIDE
    assert "pct exec 9001 -- ip -4 -o addr show" in joined  # mgmt leg got its IP?
    assert out  # a formatted diagnosis, not empty


def test_mgmt_netdiag_empty_without_helper_or_ip():
    srv = _server({})
    assert srv.mgmt_netdiag(0, "10.121.101.100") == ""
    assert srv.mgmt_netdiag(9001, "") == ""


def test_restore_total_bytes_sums_disks_from_storage_content():
    # The denominator for overall restore progress: the guest's disk sizes from the
    # storage content (works DURING the restore, unlike qm config). A disk listed
    # twice counts once; another VM's disks are ignored.
    srv = _server(
        {
            "/nodes": [{"node": "r1", "status": "online"}],
            "/nodes/r1/storage/restore-test/content": [
                {"volid": "restore-test:vm-9000-disk-0", "size": 100 * 1024**3},
                {"volid": "restore-test:vm-9000-disk-1", "size": 32 * 1024**3},
                {"volid": "restore-test:vm-9000-disk-0", "size": 100 * 1024**3},  # dup
                {"volid": "restore-test:vm-9999-disk-0", "size": 500 * 1024**3},  # other VM
            ],
        }
    )
    srv.config["target_storages"] = [{"storage": "restore-test", "use": True}]
    assert srv.restore_total_bytes(9000) == 132 * 1024**3


def test_restore_total_bytes_zero_when_nothing_found():
    srv = _server({"/nodes": [{"node": "r1", "status": "online"}]})
    srv.config["target_storages"] = [{"storage": "restore-test", "use": True}]
    assert srv.restore_total_bytes(9000) == 0


def test_probe_netdiag_includes_bridge_vm_nic_and_tcp():
    srv = _server({"/nodes": [{"node": "r1", "status": "online"}]})

    def fake_run(cmd):
        if "link show vmbr9" in cmd:
            return "vmbr9 UP"
        if "brif" in cmd:
            return "veth9001i0\ntap9000i0"
        if "qm config" in cmd:
            return "net0: virtio=BC:24:11:f0:56:79,bridge=vmbr9,tag=900"
        if "nc -z" in cmd:
            raise RuntimeError("timed out")  # 8007 not answering
        return ""

    srv.ssh.run = fake_run
    out = srv.probe_netdiag(9001, ["10.254.10.6"], restore_vmid=9000, ports=[8007])
    assert "Sandbox bridge vmbr9: UP" in out  # the sandbox bridge exists/up
    assert "veth9001i0, tap9000i0" in out  # both NICs attached to it
    assert "bridge=vmbr9, tag=900" in out  # the restored VM's NIC + VLAN
    assert "TCP 8007   no answer" in out  # the service port did not answer
    assert "||" not in out  # one fact per line, not one long line


def test_ensure_sandbox_bridge_creates_at_runtime():
    srv = _server({"/nodes": [{"node": "r1", "status": "online"}]})
    seen = []
    srv.ssh.run = lambda cmd: seen.append(cmd) or ""
    srv.ensure_sandbox_bridge()
    script = seen[0]
    assert "ip link add name vmbr9 type bridge" in script
    assert "vlan_filtering 1" in script
    assert "ip link set vmbr9 up" in script


def test_ensure_sandbox_bridge_creates_pve_native_when_absent():
    # When the node has no vmbr9 configured, write it the PVE-native way (pvesh
    # create) so it is persistent AND visible in the GUI, then bring it up at
    # runtime. We must NOT apply host-wide (`pvesh set .../network` = ifreload -a),
    # which would bounce the host's production VLAN sub-interfaces.
    srv = _server({"/nodes": [{"node": "r1", "status": "online"}]})
    orig_get = srv.pve.get

    def get(path):
        if "/network/vmbr9" in path:
            raise RuntimeError("configuration key 'vmbr9' does not exist")
        return orig_get(path)

    srv.pve.get = get
    seen = []
    srv.ssh.run = lambda cmd: seen.append(cmd) or ""
    srv.ensure_sandbox_bridge()
    joined = " ".join(seen)
    assert "pvesh create /nodes/r1/network --iface vmbr9 --type bridge" in joined
    assert "--bridge_vlan_aware 1" in joined and "--autostart 1" in joined
    assert "pvesh set /nodes/r1/network" not in joined  # no host-wide ifreload
    assert "ip link add name vmbr9 type bridge" in joined  # runtime guarantee


def test_ensure_sandbox_bridge_raises_when_host_fails():
    srv = _server({"/nodes": [{"node": "r1", "status": "online"}]})

    def boom(cmd):
        raise RuntimeError("Cannot find device")

    srv.ssh.run = boom
    with pytest.raises(RestoreTaskError):
        srv.ensure_sandbox_bridge()


def test_apply_dnsmasq_reports_failure_without_crashing(monkeypatch, tmp_path):
    # A missing dnsmasq (or any launch failure) must come back as ok:false — NOT
    # an unhandled exception that 500s the agent (the old systemctl-fallback bug).
    from app.services import restore_helper_agent as agent

    monkeypatch.setattr(agent, "DNSMASQ_CONF", str(tmp_path / "restore.conf"))
    monkeypatch.setattr(agent, "DNSMASQ_PID", str(tmp_path / "d.pid"))

    def fake_run(cmd, **k):
        if cmd and cmd[0] == "dnsmasq":
            raise FileNotFoundError(2, "No such file or directory", "dnsmasq")

        class R:
            returncode = 0
            stderr = ""
            stdout = ""

        return R()

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    out = agent.apply_dnsmasq("interface=sbx\n", [{"ip": "10.0.0.254", "prefix": 24}])
    assert out["ok"] is False
    assert "dnsmasq" in out["dnsmasq_error"]


def test_helper_agent_probe_skips_malformed_port(monkeypatch):
    # A bad port must be skipped, never fail the whole probe (→ a bare agent 500).
    from app.services import restore_helper_agent as agent

    monkeypatch.setattr(agent, "_ping", lambda ip, timeout=2.0: True)
    monkeypatch.setattr(agent, "_tcp", lambda ip, port, timeout=2.0: True)
    monkeypatch.setattr(agent, "_ensure_reachable", lambda ip, prefix=24: None)
    monkeypatch.setattr(agent, "_route_via_sbx", lambda ip: True)
    out = agent.probe_targets([{"ip": "10.0.0.50", "ports": [443, "x", None]}], require_icmp=False)
    assert out["10.0.0.50"]["ports"] == {"443": True}


def test_helper_agent_probe_refuses_when_route_not_via_sandbox(monkeypatch):
    # FAIL-CLOSED: if the guest route is NOT via the isolated sandbox leg, the probe
    # must refuse (never touch the real production VM with the same IP/MAC) and
    # report the target as dead with a reason — not silently pass.
    from app.services import restore_helper_agent as agent

    monkeypatch.setattr(agent, "_ensure_reachable", lambda ip, prefix=24: None)
    monkeypatch.setattr(agent, "_route_via_sbx", lambda ip: False)  # would egress via mgmt
    monkeypatch.setattr(agent, "_ping", lambda ip, timeout=2.0: True)
    monkeypatch.setattr(agent, "_tcp", lambda ip, port, timeout=2.0: True)
    out = agent.probe_targets([{"ip": "10.0.0.50", "ports": [443, "x"]}], require_icmp=True)
    assert out["10.0.0.50"]["icmp"] is False
    assert out["10.0.0.50"]["arp"] is False
    assert out["10.0.0.50"]["ports"] == {"443": False}  # malformed port dropped, 443 not probed
    assert "error" in out["10.0.0.50"]


def test_agent_post_surfaces_error_body(monkeypatch):
    # The agent returns {"error", "trace"} with a 500; urllib raises a bare
    # HTTPError whose str() is only "HTTP Error 500: Internal Server Error".
    # _agent_post must read the body and surface the agent's real reason.
    import io
    import urllib.error

    srv = _server({})

    def boom(req, timeout=15.0):
        raise urllib.error.HTTPError(
            req.full_url, 500, "Internal Server Error", {}, io.BytesIO(b'{"error": "boom in apply_dnsmasq"}')
        )

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(RestoreTaskError) as exc:
        srv._agent_post("10.9.0.10", "/dnsmasq", {"conf": "x"})
    assert "boom in apply_dnsmasq" in str(exc.value)
    assert "/dnsmasq" in str(exc.value)


def test_provision_helper_fails_when_dnsmasq_setup_fails():
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/storage": [{"storage": "restore-ct", "content": "images,rootdir"}],
    }
    cfg = {
        "ssh_host": "10.0.0.9",
        "api_url": "https://10.0.0.9:8006",
        "sandbox_bridge": "vmbr9",
        "mgmt_iface": "vmbr0",
        "mgmt_netmask": "255.255.255.0",
        "probe_offset": 254,
        "target_storages": [{"storage": "restore-ct", "use": True, "order": 0}],
    }
    srv = RestoreServer(cfg, FakePve(responses), FakeSsh())
    srv.ensure_helper_template = lambda: "local:vztmpl/restore-helper.tar.gz"
    srv._agent_wait_healthy = lambda ip, **k: None
    srv._agent_post = lambda ip, path, body, **k: {"ok": False, "dnsmasq_error": "address in use"}
    with pytest.raises(RestoreTaskError, match="address in use"):
        srv.provision_helper(9001, 901, "10.20.0.10", "10.0.0.1", "10.0.0.50", 24)


def test_provision_helper_appends_netdiag_when_agent_unreachable():
    # When the agent never answers, the run error must carry the inside-helper
    # network snapshot (so we can tell IP-not-applied from an L2/VLAN/ARP issue).
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/storage": [{"storage": "restore-ct", "content": "images,rootdir"}],
    }
    cfg = {
        "ssh_host": "10.0.0.9",
        "api_url": "https://10.0.0.9:8006",
        "sandbox_bridge": "vmbr9",
        "mgmt_iface": "vmbr0",
        "mgmt_netmask": "255.255.255.0",
        "mgmt_gateway": "10.20.0.1",
        "probe_offset": 254,
        "target_storages": [{"storage": "restore-ct", "use": True, "order": 0}],
    }
    srv = RestoreServer(cfg, FakePve(responses), FakeSsh())
    srv.ensure_helper_template = lambda: "local:vztmpl/restore-helper.tar.gz"

    def boom(ip, **k):
        raise RestoreTaskError("not ready within 60s")

    srv._agent_wait_healthy = boom
    with pytest.raises(RestoreTaskError, match="Helper network"):
        srv.provision_helper(9001, 901, "10.20.0.10", "10.0.0.1", "10.0.0.50", 24)
    # The diagnostic went through pct exec (host SSH), targeting the helper vmid.
    assert any("pct exec 9001" in c for c in srv.ssh.commands)


def _helper_name(data: bytes) -> str:
    import hashlib

    return f"restore-helper-{hashlib.sha256(data).hexdigest()[:8]}.tar.gz"


def test_ensure_helper_template_returns_existing_volid_without_upload(tmp_path):
    img = tmp_path / "restore-helper.tar.gz"
    img.write_bytes(b"ROOTFS")
    name = _helper_name(b"ROOTFS")
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/storage": [{"storage": "local", "content": "vztmpl,iso"}],
        "/nodes/r1/storage/local/content": [{"volid": f"local:vztmpl/{name}"}],
    }
    cfg = {"ssh_host": "10.0.0.9", "api_url": "https://10.0.0.9:8006", "helper_image_path": str(img)}
    srv = RestoreServer(cfg, FakePve(responses), FakeSsh())
    assert srv.ensure_helper_template() == f"local:vztmpl/{name}"
    assert srv.pve.uploads == []  # already present (same content hash) → no upload


def test_ensure_helper_template_prunes_stale_templates(tmp_path):
    img = tmp_path / "restore-helper.tar.gz"
    img.write_bytes(b"ROOTFS")
    name = _helper_name(b"ROOTFS")
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/storage": [{"storage": "local", "content": "vztmpl"}],
        "/nodes/r1/storage/local/content": [
            {"volid": f"local:vztmpl/{name}"},  # current → kept
            {"volid": "local:vztmpl/restore-helper-deadbeef.tar.gz"},  # stale → deleted
            {"volid": "local:vztmpl/restore-helper-cafebabe.tar.gz"},  # stale → deleted
            {"volid": "local:vztmpl/alpine-3.20.tar.zst"},  # not ours → kept
        ],
    }
    cfg = {"ssh_host": "10.0.0.9", "api_url": "https://10.0.0.9:8006", "helper_image_path": str(img)}
    srv = RestoreServer(cfg, FakePve(responses), FakeSsh())
    assert srv.ensure_helper_template() == f"local:vztmpl/{name}"
    assert srv.pve.uploads == []  # current already present → no upload
    assert sorted(srv.pve.deletes) == [
        "/nodes/r1/storage/local/content/local:vztmpl/restore-helper-cafebabe.tar.gz",
        "/nodes/r1/storage/local/content/local:vztmpl/restore-helper-deadbeef.tar.gz",
    ]


def test_ensure_helper_template_uploads_when_absent(tmp_path):
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/storage": [{"storage": "local", "content": "vztmpl"}],
        "/nodes/r1/storage/local/content": [],  # not present yet
    }
    img = tmp_path / "restore-helper.tar.gz"
    img.write_bytes(b"ROOTFS")
    name = _helper_name(b"ROOTFS")
    cfg = {"ssh_host": "10.0.0.9", "api_url": "https://10.0.0.9:8006", "helper_image_path": str(img)}
    srv = RestoreServer(cfg, FakePve(responses), FakeSsh())
    assert srv.ensure_helper_template() == f"local:vztmpl/{name}"
    assert srv.pve.uploads == [("r1", "local", name, 6)]


def test_provision_helper_fails_clearly_when_no_target_accepts_containers():
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/storage": [{"storage": "restore-vms", "content": "images"}],
    }
    cfg = {
        "ssh_host": "10.0.0.9",
        "api_url": "https://10.0.0.9:8006",
        "sandbox_bridge": "vmbr9",
        "mgmt_iface": "vmbr0",
        "mgmt_netmask": "255.255.255.0",
        "probe_offset": 254,
        "target_storages": [{"storage": "restore-vms", "use": True, "order": 0}],
    }
    srv = RestoreServer(cfg, FakePve(responses), FakeSsh())
    with pytest.raises(RuntimeError, match="rootdir"):
        srv.provision_helper(9001, 901, "10.20.0.10", "10.0.0.1", "10.0.0.50", 24)


def test_free_resources_shows_only_selected_storages():
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/status": {"memory": {"free": 5, "total": 10}},
        "/nodes/r1/storage": [
            {"storage": "restore-test", "type": "dir", "content": "images", "total": 100, "avail": 60},
            {"storage": "local-lvm", "type": "lvmthin", "content": "images", "total": 100, "avail": 90},
        ],
    }
    cfg = {
        "ssh_host": "10.0.0.9",
        "api_url": "https://10.0.0.9:8006",
        "sandbox_bridge": "vmbr9",
        "target_storages": [
            {"storage": "restore-test", "use": True, "order": 0, "disk_class": "enterprise-ssd"},
            {"storage": "local-lvm", "use": False, "order": 9999},
        ],
    }
    srv = RestoreServer(cfg, FakePve(responses), FakeSsh())
    res = srv.free_resources()
    assert [s["storage"] for s in res["stores"]] == ["restore-test"]  # the un-ticked one is hidden


def test_disk_ring_scale_ignores_network_storages():
    # The disk ring counts local disks only (/proc/diskstats), so an NFS target's
    # faster tier must not set its 100 % mark — the local SATA disk does.
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/status": {"memory": {"free": 5, "total": 10}},
        "/nodes/r1/storage": [
            {"storage": "nas-restore", "type": "nfs", "content": "images", "total": 100, "avail": 60},
            {"storage": "local-lvm", "type": "lvmthin", "content": "images", "total": 100, "avail": 90},
        ],
    }
    cfg = {
        "ssh_host": "10.0.0.9",
        "api_url": "https://10.0.0.9:8006",
        "sandbox_bridge": "vmbr9",
        "target_storages": [
            {"storage": "nas-restore", "use": True, "order": 0, "disk_class": "enterprise-ssd"},
            {"storage": "local-lvm", "use": True, "order": 1, "disk_class": "sata-hdd"},
        ],
    }
    srv = RestoreServer(cfg, FakePve(responses), FakeSsh())
    seen = {}
    srv._io_rates = lambda node, sample, disk_max_bps: seen.setdefault("max", disk_max_bps) and {}
    srv.free_resources()
    assert seen["max"] == DISK_CLASS_MAX_BPS["sata-hdd"]


def test_destroy_stops_a_running_guest_before_deleting():
    # PVE refuses to destroy a running container, so the helper lingered. Now
    # destroy stops it first, then purges.
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/lxc/9001/status/current": {"status": "running"},
    }
    srv = _server(responses)
    srv.destroy(9001, is_lxc=True, sleep=lambda s: None)
    assert ("/nodes/r1/lxc/9001/status/stop", {}) in srv.pve.posts
    assert any("/nodes/r1/lxc/9001?purge=1" in d for d in srv.pve.deletes)


def test_destroy_clears_the_lock_first():
    # A cancelled restore leaves the target VM locked ('create'), which blocks the
    # stop and the delete. destroy() must clear the lock (qm unlock) first.
    responses = {"/nodes": [{"node": "r1", "status": "online"}]}
    srv = _server(responses)
    srv.destroy(9000, sleep=lambda s: None)
    assert "qm unlock 9000" in srv.ssh.commands
    # and the unlock comes before the destroy DELETE
    assert any("/nodes/r1/qemu/9000?purge=1" in d for d in srv.pve.deletes)


def test_destroy_lxc_uses_pct_unlock():
    srv = _server({"/nodes": [{"node": "r1", "status": "online"}]})
    srv.destroy(9001, is_lxc=True, sleep=lambda s: None)
    assert "pct unlock 9001" in srv.ssh.commands


def test_stop_task_waits_until_the_task_is_stopped():
    # Aborting the qmrestore and tearing down immediately raced the lock release;
    # stop_task now DELETEs the task and waits until it actually reports stopped.
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/tasks/UPID:x/status": {"status": "stopped", "exitstatus": "OK"},
    }
    srv = _server(responses)
    srv.stop_task("UPID:x", "r1", sleep=lambda s: None)
    assert any("/tasks/UPID:x" in d for d in srv.pve.deletes)


def test_guest_agent_ips_parses_and_filters():
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/qemu/9000/agent/network-get-interfaces": {
            "result": [
                {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1", "ip-address-type": "ipv4"}]},
                {
                    "name": "eth0",
                    "ip-addresses": [
                        {"ip-address": "10.0.0.77", "ip-address-type": "ipv4"},
                        {"ip-address": "fe80::1", "ip-address-type": "ipv6"},
                    ],
                },
            ]
        },
    }
    srv = _server(responses)
    assert srv.guest_agent_ips(9000) == ["10.0.0.77"]  # loopback + ipv6 filtered out


def test_guest_agent_nics_reports_ip_and_prefix():
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/qemu/9000/agent/network-get-interfaces": {
            "result": [
                {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1", "ip-address-type": "ipv4", "prefix": 8}]},
                {
                    "name": "eth0",
                    "ip-addresses": [{"ip-address": "10.122.202.20", "ip-address-type": "ipv4", "prefix": 24}],
                },
            ]
        },
    }
    srv = _server(responses)
    # The guest's real IP with its real prefix (not always /24), loopback filtered.
    assert srv.guest_agent_nics(9000) == [("10.122.202.20", 24)]


def test_guest_agent_nics_only_real_nics_not_docker_or_wireguard():
    # A Docker host: ens18 is the VM's real NIC (its MAC is net0 in the config);
    # wg0, docker0 and the br-* bridges exist only inside the guest. Probing all of
    # them sequentially pushed a probe pass past the helper call timeout.
    def iface(name, mac, ip, prefix=24):
        return {
            "name": name,
            "hardware-address": mac,
            "ip-addresses": [{"ip-address": ip, "ip-address-type": "ipv4", "prefix": prefix}],
        }

    ifaces = [
        iface("lo", "00:00:00:00:00:00", "127.0.0.1", 8),
        iface("ens18", "00:0c:29:33:c8:ff", "100.64.0.200"),
        iface("wg0", "00:00:00:00:00:00", "192.168.223.1"),
        iface("docker0", "b6:ab:1c:e2:14:54", "172.17.0.1", 16),
    ] + [iface(f"br-{i:012x}", f"22:cc:f3:4b:3d:{i:02x}", f"172.20.{i}.1") for i in range(13)]
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/qemu/9000/agent/network-get-interfaces": {"result": ifaces},
        "/nodes/r1/qemu/9000/config": {"net0": "e1000=00:0C:29:33:C8:FF,bridge=vmbr9,mtu=1500,tag=901"},
    }
    srv = _server(responses)
    assert srv.guest_agent_nics(9000) == [("100.64.0.200", 24)]


def test_probe_http_timeout_stays_bounded():
    # The agent probes every target IP and port in PARALLEL (each capped ~2s), so
    # the HTTP budget must stay small and bounded — not grow with the port count —
    # otherwise a probe pass could stall the 5s polling cadence.
    srv = _server({})
    seen = {}
    srv._agent_post = lambda ip, path, body, timeout=15.0: seen.setdefault("timeout", timeout) and {}
    srv.probe("10.9.0.10", [{"ip": f"10.0.0.{i}", "ports": [22, 443, 8080, 8443]} for i in range(5)])
    # 5 targets × 4 ports and still a tight budget (no per-port growth).
    assert seen["timeout"] <= 15


def test_agent_post_read_timeout_is_a_restore_task_error(monkeypatch):
    import urllib.request

    def boom(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    srv = _server({})
    with pytest.raises(RestoreTaskError, match="timed out after 20s"):
        srv._agent_post("10.9.0.10", "/probe", {}, timeout=20)


_MEMINFO = """MemTotal:       1000 kB
MemFree:         100 kB
MemAvailable:    800 kB
Buffers:          50 kB
Cached:          600 kB
SReclaimable:     40 kB
"""


def test_free_resources_ram_is_available_not_pve_free():
    # PVE node status reports free=MemFree (buff/cache counts as used), which badly
    # understates what's free. free_resources must report MemAvailable instead and
    # expose the full breakdown for the hover.
    responses = {
        "/nodes": [{"node": "r1", "status": "online", "memory": {"total": 1000 * 1024, "free": 100 * 1024}}],
        "/nodes/r1/status": {"memory": {"total": 1000 * 1024, "free": 100 * 1024}},
        "/nodes/r1/storage": [],
    }
    srv = _server(responses)
    srv.ssh.run = lambda cmd: _MEMINFO if "meminfo" in cmd else ""
    res = srv.free_resources()
    assert res["ram_total"] == 1000 * 1024
    assert res["ram_free"] == 800 * 1024  # MemAvailable, not MemFree (100)
    d = res["ram_detail"]
    assert d["available"] == 800 * 1024
    assert d["free"] == 100 * 1024
    assert d["buff_cache"] == (50 + 600 + 40) * 1024
    assert d["used"] == (1000 - 100 - 690) * 1024  # total − free − buff/cache


def test_free_resources_falls_back_to_pve_when_meminfo_unreadable():
    responses = {
        "/nodes": [{"node": "r1", "status": "online"}],
        "/nodes/r1/status": {"memory": {"total": 500, "free": 200}},
        "/nodes/r1/storage": [],
    }
    srv = _server(responses)
    srv.ssh.run = lambda cmd: ""  # no meminfo
    res = srv.free_resources()
    assert res["ram_total"] == 500 and res["ram_free"] == 200 and res["ram_detail"] is None


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


# --- token provisioning (SSH-only) ---


class JsonSsh:
    """SSH stub that returns a canned stdout per issued command."""

    def __init__(self, out):
        self.out = out
        self.commands = []

    def run(self, command):
        self.commands.append(command)
        return self.out


def test_create_api_token_parses_pvesh_json():
    ssh = JsonSsh('{"full-tokenid":"root@pam!b-sentinel","info":{"privsep":"0"},"value":"sek-ret-123"}')
    srv = RestoreServer({"ssh_host": "10.0.0.9"}, FakePve({}), ssh)
    tok = srv.create_api_token()
    assert tok == {"api_user": "root@pam", "token_id": "b-sentinel", "token_secret": "sek-ret-123"}
    assert any("pvesh create" in c and "--privsep 0" in c for c in ssh.commands)


def test_build_server_tolerates_missing_api_block():
    # Token provisioning builds the server from an SSH-only config (no api_* yet).
    # build_server must not KeyError before create_api_token runs over SSH.
    from app.services import restore_orchestrator

    server = restore_orchestrator.build_server(
        {"ssh_host": "10.0.0.9", "ssh_private_key": "", "sandbox_bridge": "vmbr9"}
    )
    assert server.ssh is not None and server.pve is not None


def test_sandbox_alloc_override_cidr_honours_prefix():
    from app.services.restore_orchestrator import _sandbox_alloc

    # Manual override as CIDR: the helper is pinned to that subnet, taking the
    # lowest non-guest host; dnsmasq hands the override IP. Prefix not assumed /24.
    guest, helper, prefix = _sandbox_alloc("10.0.5.3/26", "10.9.0.10", {})
    assert guest == "10.0.5.3"
    assert prefix == 26
    assert helper == "10.0.5.1"  # a host in 10.0.5.0/26 that is not the guest


def test_sandbox_alloc_without_override_uses_isolated_fallback_not_mgmt_subnet():
    import ipaddress

    from app.services.restore_orchestrator import _sandbox_alloc

    # No override → the sandbox leg uses the dedicated isolated fallback range
    # (guest .200, helper .254), NEVER the management subnet — otherwise the
    # helper's two legs share one /24 and the management leg drops off.
    config = {"mgmt_netmask": "255.255.255.0"}
    guest, helper, prefix = _sandbox_alloc(None, "10.121.101.100", config)
    assert (guest, helper, prefix) == ("100.64.0.200", "100.64.0.254", 24)
    mgmt_net = ipaddress.ip_network("10.121.101.100/24", strict=False)
    assert ipaddress.ip_address(helper) not in mgmt_net  # no overlap with mgmt
    assert ipaddress.ip_address(guest) not in mgmt_net


def test_sandbox_alloc_bare_override_means_slash_24():
    from app.services.restore_orchestrator import _sandbox_alloc

    guest, helper, prefix = _sandbox_alloc("10.0.0.50", "10.9.0.10", {})
    assert (guest, prefix) == ("10.0.0.50", 24)
    assert helper == "10.0.0.1"


def test_parse_cidr_and_sandbox_host_ip():
    from app.services.restore_server import parse_cidr, sandbox_host_ip

    assert parse_cidr("10.0.5.3/26") == ("10.0.5.3", 26)
    assert parse_cidr("10.0.5.3") == ("10.0.5.3", 24)  # bare → /24
    assert parse_cidr("") is None
    assert parse_cidr("nonsense") is None
    # The helper takes the lowest usable host that is not the guest.
    assert sandbox_host_ip("10.0.5.3", 26) == "10.0.5.1"
    assert sandbox_host_ip("10.0.5.1", 26) == "10.0.5.2"  # guest is .1 → next host


def test_rewrite_net_line_trunks_tagged_vlan():
    # A guest that tags internally: keep the env PVID (tag) and trunk the guest VID.
    out = rewrite_net_line("virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0,tag=5", "vmbr9", 901, trunks=20)
    assert "tag=901" in out and "trunks=20" in out and "tag=5" not in out
    assert "virtio=AA:BB:CC:DD:EE:01" in out


# --- host I/O sampling (disk / network rings) ---

_DISKSTATS = """\
 259       0 nvme0n1 100 0 2000 50 200 0 4000 80 0 1500 130
 259       1 nvme0n1p1 1 0 10 1 1 0 20 1 0 30 1
   7       0 loop0 0 0 0 0 0 0 0 0 0 0 0
   8       0 sda 1 0 10 1 1 0 20 1 0 30 1
"""

_NETDEV = """\
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 123 1 0 0 0 0 0 0 123 1 0 0 0 0 0 0
  eth0: 1000 5 0 0 0 0 0 0 2000 6 0 0 0 0 0 0
"""


def test_parse_diskstats_keeps_whole_physical_devices_only():
    out = _parse_diskstats(_DISKSTATS)
    assert set(out) == {"nvme0n1", "sda"}  # partition + loop dropped
    assert out["nvme0n1"] == (2000 * 512, 4000 * 512, 1500)  # read, write bytes, busy ms


def test_parse_netdev_skips_headers_and_loopback():
    out = _parse_netdev(_NETDEV)
    assert out == {"eth0": (1000, 2000)}


def test_parse_speeds_drops_blank_and_negative():
    out = _parse_speeds("eth0 10000\nvmbr0 \nlo -1\nbad line here\n")
    assert out == {"eth0": 10000}


def test_compute_io_rates_turns_counters_into_rates():
    prev = {"t": 100.0, "disk": {"nvme0n1": (0, 0, 0)}, "net": {"eth0": (0, 0)}}
    cur = {
        "t": 102.0,  # dt = 2 s
        "disk": {"nvme0n1": (500_000_000, 200_000_000, 1500)},
        "net": {"eth0": (940_000_000, 420_000_000)},
        "link_bps": 1_250_000_000,
    }
    io = compute_io_rates(prev, cur, disk_max_bps=550_000_000)
    assert io["have"] is True
    assert io["disk_read"] == 250_000_000 and io["disk_write"] == 100_000_000
    assert io["disk_util"] == 75  # 1500 ms busy over 2000 ms window
    assert io["disk_read_pct"] == 45 and io["disk_write_pct"] == 18
    assert io["net_in"] == 470_000_000 and io["net_out"] == 210_000_000
    assert io["net_in_pct"] == 38 and io["net_out_pct"] == 17


def test_compute_io_rates_without_prior_sample_has_no_rate():
    cur = {"t": 10.0, "disk": {}, "net": {}, "link_bps": 0}
    assert compute_io_rates(None, cur, disk_max_bps=550_000_000)["have"] is False


def test_compute_io_rates_ignores_counter_reset():
    prev = {"t": 100.0, "disk": {"sda": (9, 9, 9)}, "net": {}}
    cur = {"t": 110.0, "disk": {"sda": (1, 1, 1)}, "net": {}, "link_bps": 0}  # counters went backwards
    assert compute_io_rates(prev, cur, disk_max_bps=550_000_000)["have"] is False
