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


def apply_dnsmasq(conf: str, aliases: list[dict]) -> dict:
    os.makedirs("/etc/dnsmasq.d", exist_ok=True)
    with open("/etc/dnsmasq.d/restore.conf", "w") as fh:
        fh.write(conf)
    for a in aliases:
        subprocess.run(["ip", "addr", "add", f"{a['ip']}/{a['prefix']}", "dev", SBX_IFACE], capture_output=True)
    # OpenRC first, then systemd — whichever the template ships. A failed restart
    # means DHCP won't serve, so the guest never gets its IP: surface it in the
    # reply (don't 500) so the run error names the real dnsmasq problem.
    rc = subprocess.run(["rc-service", "dnsmasq", "restart"], capture_output=True, text=True)
    if rc.returncode != 0:
        sd = subprocess.run(["systemctl", "restart", "dnsmasq"], capture_output=True, text=True)
        if sd.returncode != 0:
            return {
                "ok": False,
                "dnsmasq_error": (rc.stderr or rc.stdout or "").strip()[:400]
                or (sd.stderr or sd.stdout or "").strip()[:400]
                or "dnsmasq restart failed",
            }
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
