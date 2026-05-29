"""Dev-only reverse proxy so the Claude preview tool can screenshot the
Dockerized app. Listens on $PORT and forwards to the container on :8081.

Root "/" is rewritten to a target page (default: the seeded cluster) so a
screenshot of "/" lands on something useful. Override with $PREVIEW_ROOT.

Standard library only — no install needed. NOT for production.
"""

import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("PREVIEW_UPSTREAM", "http://127.0.0.1:8081")
ROOT_REWRITE = os.environ.get("PREVIEW_ROOT", "/clusters/pve-aboe-muc")
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


class ProxyHandler(BaseHTTPRequestHandler):
    def _proxy(self):
        path = ROOT_REWRITE if self.path == "/" else self.path
        body = None
        length = self.headers.get("Content-Length")
        if length:
            body = self.rfile.read(int(length))
        req = urllib.request.Request(UPSTREAM + path, data=body, method=self.command)
        for key, value in self.headers.items():
            if key.lower() not in HOP_BY_HOP and key.lower() != "host":
                req.add_header(key, value)
        force_email = os.environ.get("PREVIEW_FORCE_EMAIL")
        if force_email:
            req.add_header("X-Forwarded-Email", force_email)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self._relay(resp.status, resp.headers, resp.read())
        except urllib.error.HTTPError as exc:
            self._relay(exc.code, exc.headers, exc.read())

    def _relay(self, status, headers, payload):
        self.send_response(status)
        for key, value in headers.items():
            if key.lower() not in HOP_BY_HOP:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _proxy
    do_POST = _proxy

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("127.0.0.1", port), ProxyHandler).serve_forever()
