import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.web.csrf import OriginCheckMiddleware, is_cross_site

APP = "backup.example.org"


class IsCrossSiteTests(unittest.TestCase):
    def check(self, method="POST", origin=None, referer=None, host=APP, forwarded_host=None):
        headers = {"host": host}
        if origin is not None:
            headers["origin"] = origin
        if referer is not None:
            headers["referer"] = referer
        if forwarded_host:
            headers["x-forwarded-host"] = forwarded_host
        return is_cross_site(method, headers, configured_host=None)

    def test_same_origin_post_is_allowed(self):
        self.assertFalse(self.check(origin=f"https://{APP}"))
        self.assertFalse(self.check(referer=f"https://{APP}/settings"))

    def test_foreign_origin_is_rejected(self):
        self.assertTrue(self.check(origin="https://evil.example"))
        self.assertTrue(self.check(referer="https://evil.example/page"))

    def test_opaque_origin_is_rejected(self):
        self.assertTrue(self.check(origin="null"))

    def test_script_clients_without_origin_or_referer_are_allowed(self):
        # Bootstrap scripts (curl on PVE/PBS nodes) send neither header.
        self.assertFalse(self.check())

    def test_safe_methods_are_never_blocked(self):
        self.assertFalse(self.check(method="GET", origin="https://evil.example"))

    def test_host_behind_reverse_proxy(self):
        self.assertFalse(self.check(origin=f"https://{APP}", host="backup-sentinel:80", forwarded_host=APP))

    def test_configured_base_url_host_is_trusted(self):
        headers = {"host": "internal:80", "origin": f"https://{APP}"}
        self.assertFalse(is_cross_site("POST", headers, configured_host=APP))


class MiddlewareTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.add_middleware(OriginCheckMiddleware)

        @app.post("/settings/save")
        def save():
            return {"saved": True}

        self.client = TestClient(app, base_url=f"https://{APP}")

    def test_cross_site_post_gets_403(self):
        response = self.client.post("/settings/save", headers={"origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)

    def test_same_site_post_passes(self):
        response = self.client.post("/settings/save", headers={"origin": f"https://{APP}"})
        self.assertEqual(response.json(), {"saved": True})


if __name__ == "__main__":
    unittest.main()
