"""Unit tests for the active restore-server client (pure helpers + orchestration
with injected fake PVE/SSH transports; no real host)."""

import pytest

from app.services.restore_server import (
    HostIdentityError,
    RestoreServer,
    RestoreTaskError,
    _stable_mac,
    dnsmasq_config,
    helper_legs,
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


def test_helper_legs_dual_homed():
    aliases = [{"ip": "10.0.0.254", "prefix": 24}]
    nets, extra = helper_legs("vmbr9", 901, "vmbr0", "10.20.0.10", 24, None, "", aliases)
    assert "net1" in nets  # separate management leg
    assert "tag=901" in nets["net0"] and "10.0.0.254/24" in nets["net0"]
    assert "10.20.0.10/24" in nets["net1"]
    # Stable IP-derived MAC on the mgmt leg (keeps the gateway's ARP entry valid).
    assert "hwaddr=02:00:0a:14:00:0a" in nets["net1"]
    assert extra == []  # only one sandbox subnet → first used on net0


def test_helper_legs_stays_dual_homed_when_guest_in_mgmt_subnet():
    # Even when the guest IP is numerically in the management subnet, the helper
    # MUST keep a real management leg (on mgmt_iface) — the guest is on the
    # isolated sandbox bridge, so collapsing onto it would strand the mgmt IP on
    # a portless bridge (agent runs but is unreachable from the app).
    aliases = [{"ip": "10.20.0.254", "prefix": 24}]
    nets, _extra = helper_legs("vmbr9", 901, "vmbr0", "10.20.0.10", 24, None, "10.20.0.1", aliases)
    assert "net1" in nets  # management leg kept
    assert "bridge=vmbr0" in nets["net1"] and "10.20.0.10/24" in nets["net1"] and "gw=10.20.0.1" in nets["net1"]
    assert "bridge=vmbr9" in nets["net0"] and "10.20.0.254/24" in nets["net0"]  # sandbox leg serves the guest


def test_dnsmasq_config_has_reservation_and_range():
    conf = dnsmasq_config([{"mac": "aa:bb:cc:dd:ee:01", "ip": "10.0.0.50", "prefix": 24}])
    assert "dhcp-host=aa:bb:cc:dd:ee:01,10.0.0.50,infinite" in conf
    # Static-mode range (valid dnsmasq syntax): subnet, "static", netmask, lease.
    assert "dhcp-range=10.0.0.0,static,255.255.255.0,infinite" in conf
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

    def upload(self, node, storage, filename, data):
        self.uploads.append((node, storage, filename, len(data)))
        return ""  # no task UPID → no wait in the test


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
    srv.provision_helper(9001, 901, "10.20.0.10", [{"mac": "a", "ip": "10.0.0.50", "prefix": 24}])
    # Container created via the API token; rootfs on the first container-capable
    # selected target (images-only "restore-vms" skipped); dnsmasq config pushed
    # to the agent over the management IP (no SSH).
    create = [d for p, d in srv.pve.posts if p == "/nodes/r1/lxc"]
    assert create, srv.pve.posts
    assert create[0]["storage"] == "restore-ct"
    assert create[0]["ostemplate"] == "local:vztmpl/restore-helper.tar.gz"
    assert any(ip == "10.20.0.10" and path == "/dnsmasq" for ip, path, _ in posted)
    assert not any(c.startswith("pct ") for c in srv.ssh.commands)  # no pct exec


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
    out = agent.probe_targets([{"ip": "10.0.0.50", "ports": [443, 22]}], require_icmp=True)
    assert out["10.0.0.50"]["icmp"] is True
    # Key must be "ports" (the classifier reads primary["ports"]), not "tcp".
    assert out["10.0.0.50"]["ports"] == {"443": True, "22": False}


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
    out = agent.probe_targets([{"ip": "10.0.0.50", "ports": [443, "x", None]}], require_icmp=False)
    assert out["10.0.0.50"]["ports"] == {"443": True}


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
        srv.provision_helper(9001, 901, "10.20.0.10", [{"mac": "a", "ip": "10.0.0.50", "prefix": 24}])


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
    with pytest.raises(RestoreTaskError, match="helper netdiag"):
        srv.provision_helper(9001, 901, "10.20.0.10", [{"mac": "a", "ip": "10.0.0.50", "prefix": 24}])
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
        srv.provision_helper(9001, 901, "10.20.0.10", [{"mac": "a", "ip": "10.0.0.50", "prefix": 24}])


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
