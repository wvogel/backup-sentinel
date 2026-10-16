"""Where a cluster's backups go: PVE → PBS datastores → replicas.

Builds the graph behind the cluster page's topology view from three sources:
the PBS storages in the cluster's PVE config (primary targets), the sync jobs
of every registered PBS (replication edges) and datastore usage (ring fill).

A role belongs to an edge, not to a server: the same PBS can hold primary
backups in one datastore and a pull copy in another, and two PBS can back up
each other. Data is followed per namespace, so a store holding several
clusters' copies in separate namespaces only shows the ones of this cluster.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from app.services.backup_location import match_pbs_connection, normalize_fingerprint, server_name
from app.services.pbs import assess_datastore_usage

# PBS allows at most 7 namespace levels; bounds the walk through cycles.
_MAX_NS_DEPTH = 7

Node = tuple[str, str]  # (server key, datastore)


def _no_dns(_host: str) -> set[str]:
    return set()


def _join_ns(*parts: str) -> str:
    return "/".join(p for part in parts for p in part.split("/") if p)


def _map_ns(data_ns: str, source_ns: str, target_ns: str) -> str | None:
    """Namespace the data lands in on the target, or None if the job does not
    take it. Jobs sync the source namespace with its children."""
    if not source_ns or data_ns == source_ns or data_ns.startswith(f"{source_ns}/"):
        return _join_ns(target_ns, data_ns[len(source_ns) :])
    if source_ns.startswith(f"{data_ns}/") or not data_ns:
        return target_ns  # the job copies a part of this data
    return None


def _job_state(job: Mapping[str, Any]) -> str:
    state = str(job.get("last_run_state") or "")
    if not state and not job.get("last_run_at"):
        return "never"
    if state.upper() == "OK":
        return "ok"
    if state.upper().startswith("WARNINGS"):
        return "warning"
    return "failed"


class _Servers:
    """Physical PBS behind the registered connections; several clusters may
    register the same PBS, which still is one server here."""

    def __init__(self, connections: list[Mapping[str, Any]]):
        self.connections = connections
        self.key_of_conn: dict[int, str] = {}
        self.name: dict[str, str] = {}
        self.registered: set[str] = set()
        for conn in connections:
            fp = normalize_fingerprint(conn.get("fingerprint"))
            key = f"fp:{fp}" if fp else f"conn:{str(conn.get('name') or '').lower()}"
            self.key_of_conn[conn["id"]] = key
            self.name.setdefault(key, str(conn.get("name") or ""))
            self.registered.add(key)

    def resolve(self, fingerprint: str, host: str, fallback_name: str = "") -> str:
        conn = match_pbs_connection(fingerprint, host, self.connections, _no_dns)
        if conn:
            return self.key_of_conn[conn["id"]]
        label = host.strip().lower() or fallback_name
        key = f"host:{label}"
        # The remote's name in the PBS config says more than a short host name.
        self.name.setdefault(key, fallback_name or server_name(f"pbs:{label}"))
        return key


def build_topology(
    cluster_id: int,
    targets: Iterable[Mapping[str, Any]],
    connections: list[Mapping[str, Any]],
    jobs: Iterable[Mapping[str, Any]],
    usage: Iterable[Mapping[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Columns of datastore groups (by distance from the cluster) and edges."""
    servers = _Servers(connections)
    own_conns = [c for c in connections if c["cluster_id"] == cluster_id]

    usage_by_node: dict[Node, Mapping[str, Any]] = {}
    for row in usage:
        key = servers.key_of_conn.get(row["pbs_id"])
        if key:
            usage_by_node.setdefault((key, str(row["store"])), row)

    # Replication edges, once per physical server and job.
    edges_from: dict[Node, list[dict[str, Any]]] = {}
    seen_jobs: set[tuple[str, str]] = set()
    for job in jobs:
        here = servers.key_of_conn.get(job["pbs_id"])
        if not here or (here, job["job_id"]) in seen_jobs:
            continue
        seen_jobs.add((here, job["job_id"]))
        if job["remote_name"]:
            there = servers.resolve(job["remote_fingerprint"], job["remote_host"], job["remote_name"])
        else:
            there = here  # local sync between two datastores of this PBS
        local = ((here, job["store"]), job["ns"])
        remote = ((there, job["remote_store"]), job["remote_ns"])
        (src, src_ns), (dst, dst_ns) = (local, remote) if job["direction"] == "push" else (remote, local)
        edges_from.setdefault(src, []).append(
            {"job": job, "src": src, "src_ns": src_ns, "dst": dst, "dst_ns": dst_ns, "state": _job_state(job)}
        )

    depth: dict[Node, int] = {}
    edges: list[dict[str, Any]] = []
    used_jobs: set[int] = set()
    queue: deque[tuple[Node, str]] = deque()
    visited: set[tuple[Node, str]] = set()

    def reach(node: Node, ns: str, level: int) -> None:
        depth[node] = min(depth.get(node, level), level)
        if (node, ns) not in visited and ns.count("/") < _MAX_NS_DEPTH:
            visited.add((node, ns))
            queue.append((node, ns))

    def walk() -> None:
        while queue:
            node, ns = queue.popleft()
            for edge in edges_from.get(node, []):
                landed = _map_ns(ns, edge["src_ns"], edge["dst_ns"])
                if landed is None:
                    continue
                reach(edge["dst"], landed, depth[node] + 1)
                if id(edge) not in used_jobs:
                    used_jobs.add(id(edge))
                    edges.append(edge)

    primary_edges: list[dict[str, Any]] = []
    for t in targets:
        node = (servers.resolve(t["fingerprint"], t["server"]), str(t["datastore"]))
        primary_edges.append({"dst": node, "dst_ns": str(t["namespace"] or ""), "storage_id": t["storage_id"]})
        reach(node, str(t["namespace"] or ""), 1)
    walk()

    # Datastores of this cluster's PBS that no edge reaches: still show their
    # capacity, unless the connection is limited to a namespace (then the
    # other stores belong to someone else).
    for conn in own_conns:
        if conn.get("namespace"):
            continue
        key = servers.key_of_conn[conn["id"]]
        for server, store in sorted(usage_by_node):
            if server == key and (server, store) not in depth:
                reach((server, store), "", 1)
    walk()

    if not depth:
        return {"columns": [], "edges": [], "lines": []}

    own_conn_by_server = {servers.key_of_conn[c["id"]]: c for c in own_conns}
    node_ids = {node: f"ds-{i}" for i, node in enumerate(sorted(depth, key=lambda n: (depth[n], n)))}
    nodes: dict[Node, dict[str, Any]] = {}
    for node in node_ids:
        row = usage_by_node.get(node)
        total = int(row["total_bytes"]) if row else 0
        nodes[node] = {
            "id": node_ids[node],
            "store": node[1],
            "pct": round(int(row["used_bytes"]) / total * 100) if row and total else None,
            "status": assess_datastore_usage(int(row["used_bytes"]), total, row["estimated_full_at"], now)
            if row
            else "unknown",
            "used_bytes": int(row["used_bytes"]) if row else None,
            "total_bytes": total or None,
            "estimated_full_at": row["estimated_full_at"] if row else None,
            "incoming": [],
        }

    out_edges: list[dict[str, Any]] = []
    for p in primary_edges:
        out_edges.append(
            {
                "kind": "primary",
                "from": "cluster",
                "to": node_ids[p["dst"]],
                "to_ns": p["dst_ns"],
                "state": "ok",
                "storage_id": p["storage_id"],
                "back": False,
            }
        )
    for e in edges:
        job = e["job"]
        edge = {
            "kind": "sync",
            "from": node_ids[e["src"]],
            "to": node_ids[e["dst"]],
            "from_ns": e["src_ns"],
            "to_ns": e["dst_ns"],
            "state": e["state"],
            "job_id": job["job_id"],
            "schedule": job["schedule"],
            "last_run_at": job["last_run_at"],
            "last_run_state": job["last_run_state"],
            "next_run_at": job["next_run_at"],
            "source_label": f"{servers.name[e['src'][0]]}/{e['src'][1]}",
            "back": depth[e["dst"]] <= depth[e["src"]],
        }
        out_edges.append(edge)
        nodes[e["dst"]]["incoming"].append(edge)

    columns: list[list[dict[str, Any]]] = []
    for level in sorted(set(depth.values())):
        groups: dict[str, dict[str, Any]] = {}
        for node in sorted(n for n in depth if depth[n] == level):
            server = node[0]
            group = groups.setdefault(
                server,
                {
                    "server": servers.name[server],
                    "registered": server in servers.registered,
                    "conn": own_conn_by_server.get(server),
                    "stores": [],
                },
            )
            group["stores"].append(nodes[node])
        columns.append(sorted(groups.values(), key=lambda g: g["server"]))
    lines = [{k: e[k] for k in ("from", "to", "kind", "state", "back")} for e in out_edges]
    return {"columns": columns, "edges": out_edges, "lines": lines}
