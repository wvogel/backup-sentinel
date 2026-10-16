import unittest
from datetime import UTC, datetime

from app.services.pbs_topology import build_topology

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def conn(id, cluster_id, name, fingerprint="", namespace=""):
    return {
        "id": id,
        "cluster_id": cluster_id,
        "name": name,
        "api_url": f"https://{name}.int.example.de:8007",
        "fingerprint": fingerprint,
        "namespace": namespace,
    }


def usage(pbs_id, store, pct):
    return {"pbs_id": pbs_id, "store": store, "used_bytes": pct, "total_bytes": 100, "estimated_full_at": None}


def job(pbs_id, job_id, store, remote="", remote_store="", ns="", remote_ns="", state="OK", direction="pull", **kw):
    return {
        "pbs_id": pbs_id,
        "job_id": job_id,
        "direction": direction,
        "store": store,
        "ns": ns,
        "remote_name": remote,
        "remote_host": kw.get("remote_host", ""),
        "remote_fingerprint": kw.get("remote_fingerprint", ""),
        "remote_store": remote_store,
        "remote_ns": remote_ns,
        "schedule": "daily",
        "last_run_state": state,
        "last_run_at": NOW if state else None,
        "next_run_at": None,
        "remote_pbs_id": kw.get("remote_pbs_id"),
    }


def target(server, datastore, fingerprint="", namespace=""):
    return {
        "storage_id": f"st-{datastore}",
        "server": server,
        "fingerprint": fingerprint,
        "datastore": datastore,
        "namespace": namespace,
    }


ABOE = conn(1, 10, "pbs-aboe-muc", fingerprint="AA:01")
FH_FOR_ABOE = conn(2, 10, "pbs-fh-muc", fingerprint="FF:02", namespace="aboe")
PANO = conn(3, 20, "pbs-pano", fingerprint="CC:03")
FH_FOR_PANO = conn(4, 20, "pbs-fh-muc", fingerprint="FF:02", namespace="pano")


def names(topology):
    return [[(g["server"], [n["store"] for n in g["stores"]]) for g in column] for column in topology["columns"]]


class TopologyTests(unittest.TestCase):
    def build(self, cluster_id, targets, connections, jobs=(), usages=()):
        return build_topology(cluster_id, targets, list(connections), list(jobs), list(usages), now=NOW)

    def test_primary_then_pull_copy(self) -> None:
        topo = self.build(
            20,
            [target("10.0.0.3", "nas-pano", fingerprint="cc:03")],
            [ABOE, FH_FOR_ABOE, PANO, FH_FOR_PANO],
            [
                job(
                    4,
                    "s-pano",
                    "nas-pve-fh-muc",
                    ns="pano",
                    remote="pano",
                    remote_store="nas-pano",
                    remote_fingerprint="CC:03",
                )
            ],
            [usage(3, "nas-pano", 40), usage(4, "nas-pve-fh-muc", 76), usage(4, "nas-offsite-fh-muc", 18)],
        )
        self.assertEqual(names(topo), [[("pbs-pano", ["nas-pano"])], [("pbs-fh-muc", ["nas-pve-fh-muc"])]])
        primary, sync = topo["edges"]
        self.assertEqual((primary["kind"], primary["from"]), ("primary", "cluster"))
        self.assertEqual((sync["kind"], sync["state"], sync["to_ns"]), ("sync", "ok", "pano"))
        copy = topo["columns"][1][0]["stores"][0]
        self.assertEqual((copy["pct"], copy["incoming"][0]["source_label"]), (76, "pbs-pano/nas-pano"))

    def test_namespace_of_other_cluster_is_not_followed(self) -> None:
        # pbs-fh-muc pulls two namespaces into one store; the aboe copy must not
        # show up on pano's card and vice versa.
        jobs = [
            job(
                4,
                "s-pano",
                "nas-pve-fh-muc",
                ns="pano",
                remote="pano",
                remote_store="nas-pano",
                remote_fingerprint="CC:03",
            ),
            job(
                2,
                "s-aboe",
                "nas-pve-fh-muc",
                ns="aboe",
                remote="aboe",
                remote_store="nas-b1",
                remote_fingerprint="AA:01",
            ),
        ]
        topo = self.build(10, [target("pbs-aboe-muc", "nas-b1")], [ABOE, FH_FOR_ABOE, PANO, FH_FOR_PANO], jobs)
        self.assertEqual([e["job_id"] for e in topo["edges"] if e["kind"] == "sync"], ["s-aboe"])

    def test_mutual_backup_draws_back_edge_without_looping(self) -> None:
        jobs = [
            job(4, "to-fh", "nas-fh", remote="pano", remote_store="nas-pano", remote_fingerprint="CC:03"),
            job(3, "to-pano", "nas-pano", ns="fh", remote="fh", remote_store="nas-fh", remote_fingerprint="FF:02"),
        ]
        topo = self.build(20, [target("pbs-pano", "nas-pano", fingerprint="CC:03")], [PANO, FH_FOR_PANO], jobs)
        self.assertEqual(names(topo), [[("pbs-pano", ["nas-pano"])], [("pbs-fh-muc", ["nas-fh"])]])
        back = [e for e in topo["edges"] if e.get("job_id") == "to-pano"]
        self.assertEqual(len(back), 1)
        self.assertTrue(back[0]["back"])

    def test_unregistered_remote_and_local_sync(self) -> None:
        jobs = [
            job(1, "local", "nas-off", remote_store="nas-b1"),
            job(
                1,
                "push",
                "nas-b1",
                remote="offsite",
                remote_store="vault",
                direction="push",
                remote_host="backup.example.org",
                state="error: timeout",
            ),
        ]
        topo = self.build(
            10, [target("pbs-aboe-muc", "nas-b1")], [ABOE], jobs, [usage(1, "nas-b1", 83), usage(1, "nas-off", 11)]
        )
        self.assertEqual(
            names(topo),
            [[("pbs-aboe-muc", ["nas-b1"])], [("offsite", ["vault"]), ("pbs-aboe-muc", ["nas-off"])]],
        )
        unregistered = topo["columns"][1][0]
        self.assertFalse(unregistered["registered"])
        self.assertIsNone(unregistered["stores"][0]["pct"])
        states = {e["job_id"]: e["state"] for e in topo["edges"] if e["kind"] == "sync"}
        self.assertEqual((states["local"], states["push"]), ("ok", "failed"))

    def test_without_pve_targets_the_clusters_pbs_are_shown(self) -> None:
        topo = self.build(10, [], [ABOE, FH_FOR_ABOE, PANO], [], [usage(1, "nas-b1", 83), usage(2, "other", 5)])
        # pbs-fh-muc is limited to a namespace here, so its unrelated stores stay hidden.
        self.assertEqual(names(topo), [[("pbs-aboe-muc", ["nas-b1"])]])
        self.assertEqual(topo["edges"], [])

    def test_unrelated_stores_of_a_used_pbs_are_hidden(self) -> None:
        topo = self.build(
            10, [target("pbs-aboe-muc", "nas-b1")], [ABOE], [], [usage(1, "nas-b1", 83), usage(1, "spare", 2)]
        )
        self.assertEqual(names(topo), [[("pbs-aboe-muc", ["nas-b1"])]])
        self.assertEqual(len(topo["edges"]), 1)

    def test_minor_primary_target_that_is_also_a_copy_stands_behind_its_source(self) -> None:
        # pve-fh-muc backs up to pbs-fh-muc; pbs-aboe-muc pulls that into
        # nas-off, where one VM (the PBS itself) also backs up directly.
        fh = conn(6, 40, "pbs-fh-muc", fingerprint="FF:02")
        aboe = conn(7, 40, "pbs-aboe-muc", fingerprint="AA:01")
        topo = build_topology(
            40,
            [target("pbs-fh-muc", "nas-pve-fh-muc", "FF:02"), target("pbs-aboe-muc", "nas-off", "AA:01")],
            [fh, aboe],
            [job(7, "s-fh", "nas-off", remote="fh", remote_store="nas-pve-fh-muc", remote_fingerprint="FF:02")],
            [usage(6, "nas-pve-fh-muc", 76), usage(7, "nas-off", 11), usage(7, "nas-b1", 83)],
            now=NOW,
            vms_by_storage={"st-nas-pve-fh-muc": 8, "st-nas-off": 1},
        )
        self.assertEqual(names(topo), [[("pbs-fh-muc", ["nas-pve-fh-muc"])], [("pbs-aboe-muc", ["nas-off"])]])
        lines = {(line["kind"], line["to"]): line for line in topo["lines"]}
        main = lines[("primary", "ds-0")]
        side = lines[("primary", "ds-1")]
        self.assertEqual((main["minor"], main["span"]), (False, 1))
        self.assertEqual((side["minor"], side["span"]), (True, 2))
        self.assertEqual((lines[("sync", "ds-1")]["span"], lines[("sync", "ds-1")]["back"]), (1, False))

    def test_empty_without_targets_and_connections(self) -> None:
        self.assertEqual(self.build(10, [], [PANO])["columns"], [])

    def test_same_pbs_registered_twice_is_one_server(self) -> None:
        # Registered for two clusters, once without fingerprint: still one PBS,
        # and its sync job (read through both connections) is one edge.
        fh_no_fp = conn(5, 30, "pbs-fh-muc")
        jobs = [
            job(c, "s-pano", "nas-offsite-fh-muc", remote="pano", remote_store="nas-pano", remote_fingerprint="CC:03")
            for c in (4, 5)
        ]
        topo = self.build(
            20, [target("pbs-pano", "nas-pano", fingerprint="CC:03")], [PANO, FH_FOR_PANO, fh_no_fp], jobs
        )
        self.assertEqual(names(topo), [[("pbs-pano", ["nas-pano"])], [("pbs-fh-muc", ["nas-offsite-fh-muc"])]])
        self.assertEqual(len([e for e in topo["edges"] if e["kind"] == "sync"]), 1)

    def test_target_resolved_at_sync_time_joins_the_registered_pbs(self) -> None:
        # PVE knows the PBS as pbs-mar-lej, Sentinel as mgm-pbs-mar-lej.
        mgm = conn(8, 50, "mgm-pbs-mar-lej")
        aboe = conn(9, 50, "pbs-aboe-muc", fingerprint="AA:01")
        side = {**target("pbs-mar-lej", "nas-b0-volume1"), "pbs_id": 8}
        jobs = [
            job(
                9,
                "s-lej",
                "nas-off",
                remote="lej",
                remote_store="nas-b0-volume1",
                remote_host="10.1.1.1",
                remote_pbs_id=8,
            )
        ]
        topo = self.build(50, [side], [mgm, aboe], jobs, [usage(8, "nas-b0-volume1", 35)])
        self.assertEqual(names(topo), [[("mgm-pbs-mar-lej", ["nas-b0-volume1"])], [("pbs-aboe-muc", ["nas-off"])]])

    def test_certificate_fingerprints_merge_registrations(self) -> None:
        a = {**conn(10, 60, "mgm-pbs-mar-lej"), "cert_fingerprints": ["AB:CD"]}
        b = conn(11, 61, "pbs-mar-lej", fingerprint="ab:cd")
        topo = self.build(60, [target("x", "nas", fingerprint="AB:CD")], [a, b], [], [usage(11, "nas", 5)])
        self.assertEqual(names(topo), [[("mgm-pbs-mar-lej", ["nas"])]])

    def test_alias_joins_pve_target_and_registered_pbs(self) -> None:
        mgm = {**conn(12, 70, "mgm-pbs-mar-lej"), "aliases": ["pbs-mar-lej"]}
        topo = self.build(70, [target("pbs-mar-lej", "nas-b0-volume1")], [mgm], [], [usage(12, "nas-b0-volume1", 35)])
        self.assertEqual(names(topo), [[("mgm-pbs-mar-lej", ["nas-b0-volume1"])]])
        self.assertEqual(topo["columns"][0][0]["stores"][0]["pct"], 35)
