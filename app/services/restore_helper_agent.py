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
    """Give the sandbox leg an address in the target's subnet so the helper can
    reach it — needed when the IP was discovered after boot (agent/ARP) and the
    helper wasn't pre-configured for that subnet. Also pin a /32 host route for
    the target via the sandbox leg: when the guest's subnet overlaps the
    management subnet, both legs carry a connected route for it, and the pin
    forces probe traffic out the sandbox leg (where the guest actually lives)
    instead of the management leg. Idempotent; errors ignored."""
    import ipaddress

    try:
        net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
        host = net.network_address + 250
        if str(host) == ip:
            host = net.network_address + 249
        subprocess.run(["ip", "addr", "add", f"{host}/{prefix}", "dev", SBX_IFACE], capture_output=True)
        subprocess.run(["ip", "route", "replace", f"{ip}/32", "dev", SBX_IFACE], capture_output=True)
    except (ValueError, OSError):
        pass


def probe_targets(targets: list[dict], require_icmp: bool) -> dict:
    import time

    out: dict[str, dict] = {}
    for t in targets:
        ip = t.get("ip")
        if not ip:
            continue
        _ensure_reachable(ip)
        ports: dict[str, bool] = {}
        for p in t.get("ports") or []:
            try:
                port = int(p)
            except (TypeError, ValueError):
                continue  # a malformed port must not fail the whole probe
            ports[str(port)] = _tcp(ip, port)
        # Key is "ports" (not "tcp"): the classifier reads primary["ports"].
        out[ip] = {
            "icmp": _ping(ip) if require_icmp else None,
            "ports": ports,
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
