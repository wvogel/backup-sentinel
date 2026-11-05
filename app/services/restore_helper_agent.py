"""Probe/DHCP agent that runs *inside* the restore helper container.

Direction A (API-only, no SSH): the app talks to this agent over the helper's
management IP instead of `pct exec`. The agent is baked into the helper template
and started on boot (see scripts/build-helper-template.sh). It needs only the
Python 3 standard library plus `dnsmasq`, `ip` and `ping` on the container.

Endpoints (all JSON):
  GET  /health            -> {"ok": true}
  POST /dnsmasq           {"conf": "<text>", "aliases": [{"ip","prefix"}]}
                          -> writes the dnsmasq config, adds the sandbox alias
                             addresses, (re)starts dnsmasq. {"ok": true}
  POST /probe             {"targets": [{"ip","ports"}], "require_icmp": bool}
                          -> {"<ip>": {"icmp": bool, "tcp": {"<port>": bool}}}

The module is self-contained so it can be copied verbatim into the template.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AGENT_PORT = 8765
SBX_IFACE = "sbx"  # the sandbox leg inside the helper
DNSMASQ_PID = "/run/restore-dnsmasq.pid"  # pidfile of the dnsmasq we manage
DNSMASQ_CONF = "/etc/dnsmasq.d/restore.conf"  # the per-run config we write + serve


def _ping(ip: str, timeout: float = 2.0) -> bool:
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", str(int(timeout)), ip],
            capture_output=True,
            timeout=timeout + 1,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _tcp(ip: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ensure_reachable(ip: str, prefix: int = 24) -> None:
    """Give the sandbox leg the FIRST available host in the target's subnet so the
    helper can reach it — needed when the IP was discovered after boot (agent/ARP)
    and the helper wasn't pre-configured for that subnet (a static guest that kept
    its own IP). Pin a /32 host route for the target via the sandbox leg WITH an
    explicit ``src`` of that host address.

    The ``src`` is essential: the sandbox leg carries more than one subnet (its
    isolated fallback-range address plus this guest-subnet address), so without a
    pinned source the kernel may send the probe FROM the wrong address. The
    guest then answers ARP (L2) but routes its IP reply to that wrong source via
    its own default gateway — which does not exist in the isolated sandbox — so
    the reply is lost and the guest looks ARP-reachable but ICMP/TCP-dead. Pinning
    ``src`` to the guest-subnet host makes the guest reply straight back to us.
    Idempotent; errors ignored."""
    import ipaddress

    try:
        net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
        target = ipaddress.ip_address(ip)
        host = next((h for h in net.hosts() if h != target), None)
        if host is None:  # degenerate subnet with no other host
            host = target + 1
        _harden_sandbox()
        # Add the helper's sandbox-leg source as a /32, NEVER /{prefix}: when the
        # guest subnet is also the management subnet (restore guests sharing the
        # mgmt LAN), a /{prefix} here would add a second connected /24 on the helper
        # and make the mgmt leg unreachable. The /32 host route below is what carries
        # guest traffic out the isolated sandbox leg; the /32 address only gives it a
        # same-subnet source so the guest replies on-link.
        subprocess.run(["ip", "addr", "add", f"{host}/32", "dev", SBX_IFACE], capture_output=True)
        subprocess.run(["ip", "route", "replace", f"{ip}/32", "dev", SBX_IFACE, "src", str(host)], capture_output=True)
    except (ValueError, OSError):
        pass


def _harden_sandbox() -> None:
    """Per-interface ARP/RPF hardening so the helper's two legs (mgmt on the real
    LAN, sbx on the isolated bridge) can share one IP subnet without ARP flux:
    each interface answers ARP only for its own addresses (``arp_ignore=1``),
    announces the on-subnet source (``arp_announce=2``), and reverse-path filtering
    is loose (``rp_filter=2``) so the guest's reply via the /32 source is not
    dropped. Best-effort; idempotent."""
    for iface in (SBX_IFACE, "mgmt", "all"):
        for key, val in (("arp_ignore", "1"), ("arp_announce", "2"), ("rp_filter", "2")):
            try:
                subprocess.run(["sysctl", "-qw", f"net.ipv4.conf.{iface}.{key}={val}"], capture_output=True)
            except OSError:
                pass


def _route_via_sbx(ip: str) -> bool:
    """Whether the kernel would send traffic to ``ip`` out the isolated sandbox leg.
    FAIL-CLOSED guard: once the management leg shares the guest's subnet, a missing
    /32 host route would send the probe out the mgmt leg — straight at the REAL
    production VM (same IP, same MAC). We refuse to probe unless the route is via
    the sandbox leg."""
    try:
        out = subprocess.run(["ip", "route", "get", ip], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return f"dev {SBX_IFACE}" in out


def _arp_alive(ip: str) -> bool:
    """Whether the guest answers ARP for ``ip`` — proof it booted and is on the
    sandbox L2 even when its firewall drops ICMP and TCP. A Windows guest on an
    'unidentified' (public) network blocks inbound ICMP/ports but still answers
    ARP (that is below the firewall). Sending any packet makes the kernel resolve
    ARP first, so we poke the IP and then read the neighbour table for a MAC."""
    try:
        subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True, timeout=3)
    except Exception:
        pass  # the ICMP reply may be firewalled; we only want the ARP it triggers
    try:
        out = subprocess.run(
            ["ip", "neigh", "show", "to", ip], capture_output=True, text=True, timeout=3
        ).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False
    return "lladdr" in out and "failed" not in out and "incomplete" not in out


def probe_targets(targets: list[dict], require_icmp: bool) -> dict:
    import time

    out: dict[str, dict] = {}
    for t in targets:
        ip = t.get("ip")
        if not ip:
            continue
        _ensure_reachable(ip, int(t.get("prefix") or 24))
        valid_ports: list[int] = []
        for p in t.get("ports") or []:
            try:
                valid_ports.append(int(p))
            except (TypeError, ValueError):
                continue  # a malformed port must not fail the whole probe
        if not _route_via_sbx(ip):
            # Fail CLOSED: the guest route is not via the isolated sandbox leg, so a
            # probe would go out the management leg at the real production VM. Report
            # the target as dead with the reason instead of touching production.
            out[ip] = {
                "icmp": False,
                "arp": False,
                "ports": {str(p): False for p in valid_ports},
                "error": "route to guest not via sandbox leg — refusing to probe (would reach production)",
                "ts": int(time.time()),
            }
            continue
        # Key is "ports" (not "tcp"): the classifier reads primary["ports"].
        out[ip] = {
            "icmp": _ping(ip) if require_icmp else None,
            "arp": _arp_alive(ip),  # L2 liveness that survives a guest firewall (Windows)
            "ports": {str(p): _tcp(ip, p) for p in valid_ports},
            "ts": int(time.time()),
        }
    return out


def _stop_dnsmasq() -> None:
    """Stop the dnsmasq we started earlier (idempotent re-apply). Uses our own
    pidfile + os.kill so we need no service manager and no extra binary."""
    import signal

    try:
        with open(DNSMASQ_PID) as fh:
            pid = int(fh.read().strip())
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass  # not running, gone, or no pidfile: nothing to stop


def apply_dnsmasq(conf: str, aliases: list[dict]) -> dict:
    os.makedirs(os.path.dirname(DNSMASQ_CONF), exist_ok=True)
    with open(DNSMASQ_CONF, "w") as fh:
        fh.write(conf)
    for a in aliases:
        subprocess.run(["ip", "addr", "add", f"{a['ip']}/{a['prefix']}", "dev", SBX_IFACE], capture_output=True)
    # Run dnsmasq ourselves with our config — NO rc-service/systemctl: the
    # container has no systemd (Alpine/openrc), and openrc inside an LXC is
    # unreliable; the old fallback crashed on a missing `systemctl`. dnsmasq
    # daemonizes and the parent exits 0 on success, non-zero (with stderr) on a
    # config error. A failure means no DHCP → the guest never gets its IP, so we
    # report it (ok:false) rather than crash the agent (500).
    _stop_dnsmasq()
    try:
        r = subprocess.run(
            ["dnsmasq", f"--conf-file={DNSMASQ_CONF}", f"--pid-file={DNSMASQ_PID}"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return {"ok": False, "dnsmasq_error": f"cannot start dnsmasq: {exc}"}
    if r.returncode != 0:
        return {"ok": False, "dnsmasq_error": (r.stderr or r.stdout or "dnsmasq failed to start").strip()[:400]}
    return {"ok": True}


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            if self.path == "/dnsmasq":
                b = self._body()
                self._send(200, apply_dnsmasq(b.get("conf", ""), b.get("aliases") or []))
            elif self.path == "/probe":
                b = self._body()
                self._send(200, probe_targets(b.get("targets") or [], b.get("require_icmp", True)))
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:  # never crash the agent on a bad request
            # Include the traceback: the app surfaces this body verbatim, so an
            # unexpected agent-side failure names its exact cause in the run error
            # instead of a bare "HTTP 500".
            self._send(500, {"error": str(exc), "trace": traceback.format_exc()[-1200:]})

    def log_message(self, *args) -> None:
        pass


def serve(port: int = AGENT_PORT) -> None:
    ThreadingHTTPServer(("0.0.0.0", port), _Handler).serve_forever()


if __name__ == "__main__":
    serve()
