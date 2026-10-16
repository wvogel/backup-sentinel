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
    }


def target(server, datastore, fingerprint="", namespace=""):
    return {
        "storage_id": f"st-{datastore}",
        "server": server,
        "fingerprint": fingerprint,
        "datastore": datastore,
        "namespace": namespace,
    }


BERLIN = conn(1, 10, "pbs-berlin", fingerprint="AA:01")
FH_FOR_BERLIN = conn(2, 10, "pbs-hamburg", fingerprint="FF:02", namespace="berlin")
KIEL = conn(3, 20, "pbs-kiel", fingerprint="CC:03")
FH_FOR_KIEL = conn(4, 20, "pbs-hamburg", fingerprint="FF:02", namespace="kiel")


def names(topology):
    return [[(g["server"], [n["store"] for n in g["stores"]]) for g in column] for column in topology["columns"]]


class TopologyTests(unittest.TestCase):
    def build(self, cluster_id, targets, connections, jobs=(), usages=()):
        return build_topology(cluster_id, targets, list(connections), list(jobs), list(usages), now=NOW)

    def test_primary_then_pull_copy(self) -> None:
        topo = self.build(
            20,
            [target("10.0.0.3", "nas-kiel", fingerprint="cc:03")],
            [BERLIN, FH_FOR_BERLIN, KIEL, FH_FOR_KIEL],
            [
                job(
                    4,
                    "s-kiel",
                    "nas-pve-hamburg",
                    ns="kiel",
                    remote="kiel",
                    remote_store="nas-kiel",
                    remote_fingerprint="CC:03",
                )
            ],
            [usage(3, "nas-kiel", 40), usage(4, "nas-pve-hamburg", 76), usage(4, "nas-offsite-hamburg", 18)],
        )
        self.assertEqual(names(topo), [[("pbs-kiel", ["nas-kiel"])], [("pbs-hamburg", ["nas-pve-hamburg"])]])
        primary, sync = topo["edges"]
        self.assertEqual((primary["kind"], primary["from"]), ("primary", "cluster"))
        self.assertEqual((sync["kind"], sync["state"], sync["to_ns"]), ("sync", "ok", "kiel"))
        copy = topo["columns"][1][0]["stores"][0]
        self.assertEqual((copy["pct"], copy["incoming"][0]["source_label"]), (76, "pbs-kiel/nas-kiel"))

    def test_namespace_of_other_cluster_is_not_followed(self) -> None:
        # pbs-hamburg pulls two namespaces into one store; the berlin copy must not
        # show up on kiel's card and vice versa.
        jobs = [
            job(
                4,
                "s-kiel",
                "nas-pve-hamburg",
                ns="kiel",
                remote="kiel",
                remote_store="nas-kiel",
                remote_fingerprint="CC:03",
            ),
            job(
                2,
                "s-berlin",
                "nas-pve-hamburg",
                ns="berlin",
                remote="berlin",
                remote_store="nas-b1",
                remote_fingerprint="AA:01",
            ),
        ]
        topo = self.build(10, [target("pbs-berlin", "nas-b1")], [BERLIN, FH_FOR_BERLIN, KIEL, FH_FOR_KIEL], jobs)
        self.assertEqual([e["job_id"] for e in topo["edges"] if e["kind"] == "sync"], ["s-berlin"])

    def test_mutual_backup_draws_back_edge_without_looping(self) -> None:
        jobs = [
            job(4, "to-hamburg", "nas-hamburg", remote="kiel", remote_store="nas-kiel", remote_fingerprint="CC:03"),
            job(3, "to-kiel", "nas-kiel", ns="hamburg", remote="hamburg", remote_store="nas-hamburg", remote_fingerprint="FF:02"),
        ]
        topo = self.build(20, [target("pbs-kiel", "nas-kiel", fingerprint="CC:03")], [KIEL, FH_FOR_KIEL], jobs)
        self.assertEqual(names(topo), [[("pbs-kiel", ["nas-kiel"])], [("pbs-hamburg", ["nas-hamburg"])]])
        back = [e for e in topo["edges"] if e.get("job_id") == "to-kiel"]
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
            10, [target("pbs-berlin", "nas-b1")], [BERLIN], jobs, [usage(1, "nas-b1", 83), usage(1, "nas-off", 11)]
        )
        self.assertEqual(
            names(topo),
            [[("pbs-berlin", ["nas-b1"])], [("offsite", ["vault"]), ("pbs-berlin", ["nas-off"])]],
        )
        unregistered = topo["columns"][1][0]
        self.assertFalse(unregistered["registered"])
        self.assertIsNone(unregistered["stores"][0]["pct"])
        states = {e["job_id"]: e["state"] for e in topo["edges"] if e["kind"] == "sync"}
        self.assertEqual((states["local"], states["push"]), ("ok", "failed"))

    def test_without_pve_targets_the_clusters_pbs_are_shown(self) -> None:
        topo = self.build(10, [], [BERLIN, FH_FOR_BERLIN, KIEL], [], [usage(1, "nas-b1", 83), usage(2, "other", 5)])
        # pbs-hamburg is limited to a namespace here, so its unrelated stores stay hidden.
        self.assertEqual(names(topo), [[("pbs-berlin", ["nas-b1"])]])
        self.assertEqual(topo["edges"], [])

    def test_unreached_store_of_own_pbs_is_listed_unconnected(self) -> None:
        topo = self.build(
            10, [target("pbs-berlin", "nas-b1")], [BERLIN], [], [usage(1, "nas-b1", 83), usage(1, "spare", 2)]
        )
        self.assertEqual(names(topo), [[("pbs-berlin", ["nas-b1", "spare"])]])
        self.assertEqual(len(topo["edges"]), 1)

    def test_empty_without_targets_and_connections(self) -> None:
        self.assertEqual(self.build(10, [], [KIEL])["columns"], [])

    def test_same_pbs_registered_twice_is_one_server(self) -> None:
        # Registered for two clusters, once without fingerprint: still one PBS,
        # and its sync job (read through both connections) is one edge.
        fh_no_fp = conn(5, 30, "pbs-hamburg")
        jobs = [
            job(c, "s-kiel", "nas-offsite-hamburg", remote="kiel", remote_store="nas-kiel", remote_fingerprint="CC:03")
            for c in (4, 5)
        ]
        topo = self.build(
            20, [target("pbs-kiel", "nas-kiel", fingerprint="CC:03")], [KIEL, FH_FOR_KIEL, fh_no_fp], jobs
        )
        self.assertEqual(names(topo), [[("pbs-kiel", ["nas-kiel"])], [("pbs-hamburg", ["nas-offsite-hamburg"])]])
        self.assertEqual(len([e for e in topo["edges"] if e["kind"] == "sync"]), 1)
