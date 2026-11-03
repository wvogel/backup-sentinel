"""green_check_labels: the checks already green across any probed IP, for the
live restore card — liveness (ICMP/ARP) first, then open ports in numeric order."""

import unittest

from app.services.restore_probe import green_check_labels as g


class GreenCheckLabelsTests(unittest.TestCase):
    def test_icmp_and_open_port(self) -> None:
        self.assertEqual(g({"10.0.0.5": {"icmp": True, "ports": {"443": True, "445": False}}}), ["ICMP", "443"])

    def test_arp_only_when_no_icmp(self) -> None:
        self.assertEqual(g({"10.0.0.5": {"icmp": False, "arp": True, "ports": {}}}), ["ARP"])

    def test_union_across_ips_ports_sorted(self) -> None:
        checks = {
            "a": {"icmp": False, "ports": {"443": True}},
            "b": {"arp": True, "ports": {"443": False, "80": True}},
        }
        self.assertEqual(g(checks), ["ARP", "80", "443"])

    def test_nothing_green_or_non_probe_keys(self) -> None:
        self.assertEqual(g({}), [])
        self.assertEqual(g({"note": "no target IP yet"}), [])
        self.assertEqual(g({"10.0.0.5": {"icmp": False, "ports": {"443": False}}}), [])


if __name__ == "__main__":
    unittest.main()
