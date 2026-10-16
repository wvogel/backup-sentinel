import unittest

from app.services.backup_location import (
    assign_pbs_servers,
    connection_location,
    match_pbs_connection,
    pbs_location,
    pve_location,
    resolve_pbs_connection,
    server_name,
    server_of,
)


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


AOBE = {"name": "pbs-aboe-muc", "api_url": "https://pbs-aboe-muc.example.org:8007", "fingerprint": "AA:BB:CC"}
FH = {"name": "pbs-fh-muc", "api_url": "https://10.9.9.9:8007", "fingerprint": "DD:EE:FF"}


def pve_event(fingerprint="", server="", datastore="nas-b1", namespace=""):
    return {
        "vmid": 100,
        "location": pbs_location(fingerprint, server, datastore, namespace),
        "pbs_target": {"fingerprint": fingerprint, "server": server, "datastore": datastore, "namespace": namespace},
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

    def test_pve_storage_matches_by_fingerprint_in_any_notation(self):
        self.assertEqual(self.assign(pve_event("aabbcc", "10.0.0.5")), ["pbs:pbs-aboe-muc"])

    def test_pve_storage_without_fingerprint_matches_by_hostname(self):
        # ACME certificate: PVE needs no fingerprint, only the server name.
        self.assertEqual(self.assign(pve_event("", "PBS-ABOE-MUC.example.org")), ["pbs:pbs-aboe-muc"])
        self.assertEqual(self.assign(pve_event("", "pbs-aboe-muc")), ["pbs:pbs-aboe-muc"])

    def test_pve_storage_matches_by_resolved_ip(self):
        dns = {"10.0.0.5": {"10.0.0.5"}, "pbs-aboe-muc.example.org": {"10.0.0.5"}}
        result = self.assign(pve_event("", "10.0.0.5"), resolve=lambda h: dns.get(h, set()))
        self.assertEqual(result, ["pbs:pbs-aboe-muc"])

    def test_ip_is_not_shortened_to_its_first_octet(self):
        self.assertEqual(self.assign(pve_event("", "10.0.0.7")), ["pbs:10.0.0.7"])

    def test_unregistered_pbs_is_named_by_host_and_merged_by_fingerprint(self):
        result = self.assign(pve_event("11:22", "offsite.example.org"), pve_event("1122", "192.0.2.4"))
        self.assertEqual(result[0], result[1])
        self.assertEqual(result[0], "pbs:offsite.example.org")

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


if __name__ == "__main__":
    unittest.main()


MGM = {
    "id": 9,
    "name": "mgm-pbs-mar-lej",
    "api_url": "https://10.9.0.2:8007",
    "fingerprint": "",
    "cert_fingerprints": ["AB:CD:EF"],
}


class CertificateMatchTests(unittest.TestCase):
    def test_certificate_fingerprint_reported_by_the_pbs_matches(self):
        self.assertIs(match_pbs_connection("ab:cd:ef", "pbs-mar-lej", [MGM], no_dns), MGM)

    def test_probe_reads_the_certificate_when_pve_has_no_fingerprint(self):
        probed = []

        def probe(host, port):
            probed.append((host, port))
            return "AB:CD:EF"

        conn = resolve_pbs_connection("", "pbs-mar-lej", [MGM], no_dns, probe)
        self.assertIs(conn, MGM)
        self.assertEqual(probed, [("pbs-mar-lej", 8007)])

    def test_probe_is_skipped_when_names_already_match(self):
        def probe(host, port):
            raise AssertionError("no probe needed")

        self.assertIs(resolve_pbs_connection("", "mgm-pbs-mar-lej", [MGM], no_dns, probe), MGM)

    def test_unreachable_probe_matches_nothing(self):
        self.assertIsNone(resolve_pbs_connection("", "pbs-mar-lej", [MGM], no_dns, lambda h, p: ""))
