import unittest

from app.services.backup_location import pbs_location, pve_location, server_of


class BackupLocationTests(unittest.TestCase):
    FP = "AB:CD:EF:01"

    def test_same_pbs_via_ip_and_hostname_is_one_server(self):
        via_pve = pbs_location(self.FP, "10.0.0.5", "nas-b1", "aboe")
        via_direct = pbs_location(self.FP.lower(), "pbs.example.org", "nas-b1")
        self.assertEqual(server_of(via_pve), server_of(via_direct))

    def test_host_is_used_without_fingerprint(self):
        self.assertEqual(server_of(pbs_location("", "PBS.example.org", "ds")), "pbs:pbs.example.org")

    def test_different_pbs_are_different_servers(self):
        self.assertNotEqual(
            server_of(pbs_location("aa:aa", "pbs1", "ds")), server_of(pbs_location("bb:bb", "pbs2", "ds"))
        )

    def test_pve_storages_are_their_own_location(self):
        self.assertEqual(server_of(pve_location("nfs-backup")), "pve:nfs-backup")


if __name__ == "__main__":
    unittest.main()
