"""Probe agent + result classification for restore testing.

The helper container runs a small **probe agent** on the isolated sandbox L2:
ICMP echo and TCP socket connects against the restored VM's IP(s). It returns
structured JSON; the worker collects it over the helper's **management** leg
(spec §10). This module holds the embedded agent script, a thin worker-side
client (injectable transport, so it is testable without a network), and the
pure result classifier (spec §13).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

RestoreResult = Literal["passed", "limited", "failed"]

# The probe agent script, deployed into the helper LXC. Kept dependency-free
# (BusyBox/Python3 stdlib) so it runs in a tiny image. It reads a JSON request
# {"targets": [{"ip": "...", "ports": [..]}]} and prints one JSON result object:
# {"<ip>": {"icmp": bool, "ports": {"<port>": bool}, "latency_ms": float|None, "ts": epoch}}.
PROBE_AGENT_SCRIPT = r"""#!/usr/bin/env python3
import json, socket, subprocess, sys, time

def icmp(ip):
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False

def tcp(ip, port, timeout=2.0):
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True
    except Exception:
        return False

def main():
    req = json.load(sys.stdin)
    out = {}
    for t in req.get("targets", []):
        ip = t["ip"]
        start = time.time()
        ok = icmp(ip)
        out[ip] = {
            "icmp": ok,
            "latency_ms": round((time.time() - start) * 1000, 1) if ok else None,
            "ports": {str(p): tcp(ip, p) for p in t.get("ports", [])},
            "ts": int(time.time()),
        }
    json.dump(out, sys.stdout)

if __name__ == "__main__":
    main()
"""


def classify(
    primary_result: dict[str, Any] | None,
    *,
    require_icmp: bool,
    tcp_ports: list[int],
    had_ip: bool,
) -> RestoreResult:
    """Classify a run from the primary IP's probe result (spec §13).

    - ``passed``  — ICMP ok (unless not required) AND every configured port open.
    - ``failed``  — nothing answered at all.
    - ``limited`` — booted but no IP to probe, or something answered but not all
      that was required.
    """
    if not had_ip or primary_result is None:
        return "limited"

    icmp_ok = bool(primary_result.get("icmp")) or not require_icmp
    ports = primary_result.get("ports", {})
    ports_ok = all(ports.get(str(p), False) for p in tcp_ports)

    if icmp_ok and ports_ok:
        return "passed"

    reachable_anything = bool(primary_result.get("icmp")) or any(ports.get(str(p), False) for p in tcp_ports)
    if not reachable_anything:
        return "failed"
    return "limited"


def classify_multi(
    checks: dict[str, Any],
    ips: list[str],
    *,
    require_icmp: bool,
    tcp_ports: list[int],
    had_ip: bool,
) -> RestoreResult:
    """Classify a run across SEVERAL candidate IPs (the guest agent may report
    more than one IPv4). A check counts as green if **any** IP satisfied it:

    - ``passed``  — ICMP green on some IP (unless not required) AND every port
      green on some IP.
    - ``failed``  — nothing answered on any IP.
    - ``limited`` — no IP to probe, or something answered but not all required.

    This is the union across IPs; the caller keeps retrying until the first pass
    where every check is green (or the window elapses)."""
    if not had_ip or not ips:
        return "limited"
    results = [checks.get(ip) or {} for ip in ips]
    icmp_any = any(bool(r.get("icmp")) for r in results)
    icmp_ok = icmp_any or not require_icmp
    ports_ok = all(any((r.get("ports") or {}).get(str(p), False) for r in results) for p in tcp_ports)
    if icmp_ok and ports_ok:
        return "passed"
    reachable_anything = icmp_any or any((r.get("ports") or {}).get(str(p), False) for r in results for p in tcp_ports)
    return "limited" if reachable_anything else "failed"


def probe_once(
    mgmt_ip: str,
    token: str,
    targets: list[dict[str, Any]],
    *,
    transport: Callable[[str, str, list[dict[str, Any]]], dict[str, Any]],
) -> dict[str, Any]:
    """Query the helper's probe agent over its management IP for one pass.

    ``transport(mgmt_ip, token, targets) -> result-json`` is injected so this is
    testable without a network; in production it is an authenticated HTTP(S)
    call (per-run token) or an SSH→``pct exec`` of the agent script.
    """
    return transport(mgmt_ip, token, targets)
