from app.services.netdiag_format import format_netdiag, format_run_log

LEGACY = (
    "probe netdiag [bridge: vmbr9  UP  fe:1b:a6:bb:8c:20 <BROADCAST,UP> || bridge ports: tap9000i0 veth9001i0 || "
    "bridge vlans: port vlan-id nic2 1 PVID Egress Untagged 2 3 4 || "
    "vm nic: net0: virtio=BC:24:11:89:5B:36,bridge=vmbr9,tag=900 || "
    "sbx addr: 2: sbx    inet 10.254.2.1/24 scope global sbx || "
    "routes: default via 10.121.101.254 dev mgmt metric 1 onlink  10.254.2.0/24 dev sbx proto kernel scope link src 10.254.2.1 || "
    "ping 10.254.2.14: 64 bytes from 10.254.2.14: icmp_seq=1 ttl=128 time=0.251 ms || "
    "neigh 10.254.2.14: 10.254.2.14 dev sbx lladdr bc:24:11:89:5b:36 REACHABLE || "
    "tcp 10.254.2.14:3389: OK || tcp 10.254.2.14:8080: ERR ssh command failed: ]"
)


def test_legacy_one_line_log_is_rerendered():
    out = format_run_log(LEGACY)
    assert "Sandbox bridge vmbr9: UP" in out
    assert "tap9000i0, veth9001i0" in out
    assert "nic2" not in out  # VLAN rows of ports outside the sandbox bridge are dropped
    assert "addresses  10.254.2.1/24" in out
    # routes split back into one per line, "via <ip>" not split off
    assert "routes     default via 10.121.101.254 dev mgmt metric 1 onlink\n" in out
    assert "\n             10.254.2.0/24 dev sbx" in out
    assert "ICMP       answers (0.251 ms)" in out
    assert "TCP 3389   open" in out and "TCP 8080   no answer" in out


def test_other_logs_pass_through():
    assert format_run_log("something else") == "something else"
    assert format_run_log(None) == ""


def test_vlans_compressed_and_filtered_to_bridge_ports():
    out = format_netdiag(
        [
            ("bridge", "vmbr9 UP"),
            ("bridge ports", "tap9000i0\nveth9001i0"),
            (
                "bridge vlans",
                "port vlan-id\nnic2 1 PVID Egress Untagged\n 2-39\ntap9000i0 900 PVID Egress Untagged\nveth9001i0 900\n 901\n 902",
            ),
        ]
    )
    assert "tap9000i0: 900 (PVID, untagged)" in out
    assert "veth9001i0: 900–902" in out
    assert "nic2" not in out


def test_arp_incomplete_explains_l2_problem():
    out = format_netdiag([("neigh 10.0.0.5", "10.0.0.5 dev sbx INCOMPLETE")])
    assert "INCOMPLETE — no MAC" in out
