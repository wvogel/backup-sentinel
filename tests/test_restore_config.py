"""Tests for restore-server config (DB-gated: uses app_settings)."""

import os
import unittest

if os.environ.get("BSENTINEL_DB_TESTS") != "1":
    raise unittest.SkipTest("set BSENTINEL_DB_TESTS=1 to run database tests (they truncate all tables)")

from app import db
from app.services import restore_config


def _payload(**over):
    base = dict(
        name="PVE-Restore",
        api_url="https://restore.example:8006",
        api_user="root@pam",
        token_id="restore",
        token_secret="super-secret",
        ssh_host="10.0.0.9",
        ssh_private_key="-----BEGIN KEY-----\nabc\n-----END KEY-----",
        mgmt_iface="vmbr0",
        mgmt_netmask="255.255.255.0",
        mgmt_ip_from="10.9.0.10",
        mgmt_ip_to="10.9.0.20",
        target_storages=[{"storage": "zfs-fast", "type": "zfspool", "disk_class": "enterprise-ssd", "order": 0}],
    )
    base.update(over)
    return base


class RestoreConfigTestCase(unittest.TestCase):
    def setUp(self) -> None:
        db.init_db()
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE app_settings")
            conn.commit()

    def test_not_configured_initially(self) -> None:
        self.assertFalse(restore_config.is_configured())
        self.assertIsNone(restore_config.load())

    def test_roundtrip_encrypts_secrets(self) -> None:
        restore_config.save(_payload())
        self.assertTrue(restore_config.is_configured())
        # Raw stored secret must not be plaintext.
        raw = db.get_settings_dict("restore_server.")
        self.assertNotEqual(raw["restore_server.token_secret"], "super-secret")
        # load() decrypts.
        cfg = restore_config.load()
        self.assertEqual(cfg["token_secret"], "super-secret")
        self.assertEqual(cfg["target_storages"][0]["disk_class"], "enterprise-ssd")

    def test_empty_secret_keeps_previous(self) -> None:
        restore_config.save(_payload())
        restore_config.save(_payload(token_secret="", ssh_private_key=""))
        cfg = restore_config.load()
        self.assertEqual(cfg["token_secret"], "super-secret")

    def test_public_config_strips_secrets(self) -> None:
        restore_config.save(_payload())
        pub = restore_config.public_config()
        self.assertNotIn("token_secret", pub)
        self.assertTrue(pub["has_token_secret"])

    def test_invalid_ip_range_rejected(self) -> None:
        with self.assertRaises(ValueError):
            restore_config.save(_payload(mgmt_ip_from="10.9.0.20", mgmt_ip_to="10.9.0.10"))
