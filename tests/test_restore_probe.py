"""Pure-logic tests for probe classification + the injectable client."""

from app.services.restore_probe import classify, classify_multi, probe_once


def test_passed_requires_icmp_and_all_ports():
    res = {"icmp": True, "ports": {"443": True, "445": True}}
    assert classify(res, require_icmp=True, tcp_ports=[443, 445], had_ip=True) == "passed"


def test_limited_when_no_ip():
    assert classify(None, require_icmp=True, tcp_ports=[], had_ip=False) == "limited"


def test_limited_when_a_port_closed():
    res = {"icmp": True, "ports": {"443": True, "445": False}}
    assert classify(res, require_icmp=True, tcp_ports=[443, 445], had_ip=True) == "limited"


def test_failed_when_nothing_reachable():
    res = {"icmp": False, "ports": {"443": False}}
    assert classify(res, require_icmp=True, tcp_ports=[443], had_ip=True) == "failed"


def test_icmp_not_required_passes_on_ports_only():
    res = {"icmp": False, "ports": {"443": True}}
    assert classify(res, require_icmp=False, tcp_ports=[443], had_ip=True) == "passed"


def test_icmp_required_but_down_with_open_port_is_limited():
    res = {"icmp": False, "ports": {"443": True}}
    assert classify(res, require_icmp=True, tcp_ports=[443], had_ip=True) == "limited"


def test_arp_satisfies_liveness_when_icmp_is_firewalled():
    # A Windows guest drops ICMP on its public-network firewall but answers ARP.
    # ARP alive + open port → passed, even with require_icmp.
    res = {"icmp": False, "arp": True, "ports": {"3389": True}}
    assert classify(res, require_icmp=True, tcp_ports=[3389], had_ip=True) == "passed"


def test_arp_alone_passes_when_no_ports_required():
    res = {"icmp": False, "arp": True, "ports": {}}
    assert classify(res, require_icmp=True, tcp_ports=[], had_ip=True) == "passed"


def test_classify_multi_greens_each_check_across_ips():
    # ICMP/ARP alive on one IP, the port open on another → all checks green.
    checks = {
        "10.0.0.6": {"icmp": False, "arp": True, "ports": {"443": False}},
        "10.0.9.9": {"icmp": False, "arp": False, "ports": {"443": True}},
    }
    res = classify_multi(checks, ["10.0.0.6", "10.0.9.9"], require_icmp=True, tcp_ports=[443], had_ip=True)
    assert res == "passed"


def test_classify_multi_failed_when_nothing_answers():
    checks = {"10.0.0.6": {"icmp": False, "arp": False, "ports": {"443": False}}}
    assert classify_multi(checks, ["10.0.0.6"], require_icmp=True, tcp_ports=[443], had_ip=True) == "failed"


def test_probe_once_uses_injected_transport():
    seen = {}

    def fake(mgmt_ip, token, targets):
        seen["args"] = (mgmt_ip, token, targets)
        return {"10.0.0.1": {"icmp": True, "ports": {}}}

    out = probe_once("10.9.0.10", "tok", [{"ip": "10.0.0.1", "ports": []}], transport=fake)
    assert out["10.0.0.1"]["icmp"] is True
    assert seen["args"][0] == "10.9.0.10"
