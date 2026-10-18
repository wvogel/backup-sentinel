import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.web.routes_bootstrap import router


class BootstrapScriptRouteTests(unittest.TestCase):
    """The commands in the settings fetch these scripts; without them no node can be onboarded."""

    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def test_proxmox_script_is_served(self) -> None:
        response = self.client.get("/bootstrap/proxmox-agent.sh")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/x-shellscript", response.headers["content-type"])
        self.assertIn("pveum aclmod / -user", response.text)

    def test_pbs_script_is_served(self) -> None:
        response = self.client.get("/bootstrap/pbs-agent.sh")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/x-shellscript", response.headers["content-type"])
        self.assertIn("acl update / Audit", response.text)
