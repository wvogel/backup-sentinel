"""Pure-logic tests for probe classification + the injectable client."""

from app.services.restore_probe import classify, probe_once


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


def test_probe_once_uses_injected_transport():
    seen = {}

    def fake(mgmt_ip, token, targets):
        seen["args"] = (mgmt_ip, token, targets)
        return {"10.0.0.1": {"icmp": True, "ports": {}}}

    out = probe_once("10.9.0.10", "tok", [{"ip": "10.0.0.1", "ports": []}], transport=fake)
    assert out["10.0.0.1"]["icmp"] is True
    assert seen["args"][0] == "10.9.0.10"
