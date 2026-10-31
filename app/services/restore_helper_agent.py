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
import socket
import subprocess
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
    helper wasn't pre-configured for that subnet. Idempotent; errors ignored."""
    import ipaddress

    try:
        net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
        host = net.network_address + 250
        if str(host) == ip:
            host = net.network_address + 249
        subprocess.run(["ip", "addr", "add", f"{host}/{prefix}", "dev", SBX_IFACE], capture_output=True)
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
        # Key is "ports" (not "tcp"): the classifier reads primary["ports"].
        out[ip] = {
            "icmp": _ping(ip) if require_icmp else None,
            "ports": {str(p): _tcp(ip, int(p)) for p in (t.get("ports") or [])},
            "ts": int(time.time()),
        }
    return out


def apply_dnsmasq(conf: str, aliases: list[dict]) -> None:
    with open("/etc/dnsmasq.d/restore.conf", "w") as fh:
        fh.write(conf)
    for a in aliases:
        subprocess.run(["ip", "addr", "add", f"{a['ip']}/{a['prefix']}", "dev", SBX_IFACE], capture_output=True)
    # OpenRC first, then systemd — whichever the template ships.
    if subprocess.run(["rc-service", "dnsmasq", "restart"], capture_output=True).returncode != 0:
        subprocess.run(["systemctl", "restart", "dnsmasq"], capture_output=True)


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
                apply_dnsmasq(b.get("conf", ""), b.get("aliases") or [])
                self._send(200, {"ok": True})
            elif self.path == "/probe":
                b = self._body()
                self._send(200, probe_targets(b.get("targets") or [], b.get("require_icmp", True)))
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:  # never crash the agent on a bad request
            self._send(500, {"error": str(exc)})

    def log_message(self, *args) -> None:
        pass


def serve(port: int = AGENT_PORT) -> None:
    ThreadingHTTPServer(("0.0.0.0", port), _Handler).serve_forever()


if __name__ == "__main__":
    serve()
