import unittest

from app.services.backup_location import (
    assign_pbs_servers,
    connection_location,
    match_pbs_connection,
    pbs_location,
    pve_location,
    server_name,
)


def server_of(location: str) -> str:
    """Server part of a location, as the copy count groups them."""
    return location.split("/", 1)[0]


class BackupLocationTests(unittest.TestCase):
    def test_host_is_the_server_key(self):
        self.assertEqual(server_of(pbs_location("PBS.example.org", "ds")), "pbs:pbs.example.org")

    def test_host_and_namespace_build_the_location(self):
        self.assertEqual(pbs_location("pbs1", "nas-b1", "aboe"), "pbs:pbs1/nas-b1/aboe")
        self.assertEqual(pbs_location("pbs1", "nas-b1"), "pbs:pbs1/nas-b1")

    def test_different_hosts_are_different_servers(self):
        self.assertNotEqual(server_of(pbs_location("pbs1", "ds")), server_of(pbs_location("pbs2", "ds")))

    def test_pve_storages_are_their_own_location(self):
        self.assertEqual(server_of(pve_location("nfs-backup")), "pve:nfs-backup")


AOBE = {"name": "pbs-aboe-muc", "api_url": "https://pbs-aboe-muc.example.org:8007"}
FH = {"name": "pbs-fh-muc", "api_url": "https://10.9.9.9:8007"}


def pve_event(server="", datastore="nas-b1", namespace=""):
    return {
        "vmid": 100,
        "location": pbs_location(server, datastore, namespace),
        "pbs_target": {"server": server, "datastore": datastore, "namespace": namespace},
    }


def no_dns(host):
    return set()


class AssignPbsServersTests(unittest.TestCase):
    def assign(self, *events, resolve=no_dns):
        events = list(events)
        assign_pbs_servers(events, [AOBE, FH], resolve=resolve)
        return [server_of(e["location"]) for e in events]

    def test_direct_connection_location_uses_its_name(self):
        self.assertEqual(server_of(connection_location(AOBE, "nas-b1")), "pbs:pbs-aboe-muc")

    def test_pve_storage_matches_by_hostname(self):
        # ACME certificate / rotating certs: matched purely by the server name.
        self.assertEqual(self.assign(pve_event("PBS-ABOE-MUC.example.org")), ["pbs:pbs-aboe-muc"])
        self.assertEqual(self.assign(pve_event("pbs-aboe-muc")), ["pbs:pbs-aboe-muc"])

    def test_pve_storage_matches_by_resolved_ip(self):
        dns = {"10.0.0.5": {"10.0.0.5"}, "pbs-aboe-muc.example.org": {"10.0.0.5"}}
        result = self.assign(pve_event("10.0.0.5"), resolve=lambda h: dns.get(h, set()))
        self.assertEqual(result, ["pbs:pbs-aboe-muc"])

    def test_ip_is_not_shortened_to_its_first_octet(self):
        self.assertEqual(self.assign(pve_event("10.0.0.7")), ["pbs:10.0.0.7"])

    def test_unregistered_pbs_is_named_by_its_host(self):
        self.assertEqual(self.assign(pve_event("offsite.example.org")), ["pbs:offsite.example.org"])

    def test_same_unregistered_host_is_one_server(self):
        result = self.assign(pve_event("offsite.example.org"), pve_event("offsite.example.org", namespace="ns2"))
        self.assertEqual(server_of_both := {result[0], result[1]}, {"pbs:offsite.example.org"})
        self.assertEqual(len(server_of_both), 1)

    def test_other_events_are_untouched(self):
        event = {"vmid": 1, "location": pve_location("nfs")}
        assign_pbs_servers([event], [AOBE], resolve=no_dns)
        self.assertEqual(event["location"], "pve:nfs")

    def test_server_name_strips_the_kind(self):
        self.assertEqual(server_name("pbs:pbs-aboe-muc"), "pbs-aboe-muc")
        self.assertEqual(server_name("pve:nfs-backup"), "nfs-backup")

    def test_server_name_is_the_short_host_name(self):
        self.assertEqual(server_name("pbs:pbs-aboe-muc.int.smileeyes.de"), "pbs-aboe-muc")
        self.assertEqual(server_name("pbs:192.0.2.4"), "192.0.2.4")


MGM = {"id": 9, "name": "mgm-pbs-mar-lej", "api_url": "https://10.9.0.2:8007", "aliases": ["pbs-mar-lej", "10.20.0.2"]}


class MatchByHostTests(unittest.TestCase):
    def test_registered_name_matches(self):
        self.assertIs(match_pbs_connection("mgm-pbs-mar-lej", [MGM], no_dns), MGM)

    def test_alias_name_matches(self):
        self.assertIs(match_pbs_connection("PBS-MAR-LEJ.int.example.de", [MGM], no_dns), MGM)

    def test_alias_ip_matches(self):
        self.assertIs(match_pbs_connection("10.20.0.2", [MGM], no_dns), MGM)

    def test_nothing_matches_an_unknown_host(self):
        self.assertIsNone(match_pbs_connection("unknown.example.org", [MGM], no_dns))


if __name__ == "__main__":
    unittest.main()
