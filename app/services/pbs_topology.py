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
from urllib.parse import urlsplit

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
        # Connections sharing a fingerprint, name or API host are one server,
        # even if one of them was registered without a fingerprint.
        owner: dict[str, int] = {}
        parent = {conn["id"]: conn["id"] for conn in connections}

        def root(conn_id: int) -> int:
            while parent[conn_id] != conn_id:
                conn_id = parent[conn_id]
            return conn_id

        for conn in connections:
            fps = {normalize_fingerprint(f) for f in [conn.get("fingerprint"), *(conn.get("cert_fingerprints") or [])]}
            host = urlsplit(str(conn.get("api_url") or "")).hostname or ""
            idents = [f"fp:{fp}" for fp in sorted(fps) if fp]
            aliases = [f"host:{str(a).lower()}" for a in conn.get("aliases") or []]
            for ident in (*idents, f"name:{str(conn.get('name') or '').lower()}", f"host:{host}", *aliases):
                if not ident or ident.endswith(":"):
                    continue
                if ident in owner:
                    parent[root(conn["id"])] = root(owner[ident])
                else:
                    owner[ident] = conn["id"]
        for conn in connections:
            key = f"pbs:{root(conn['id'])}"
            self.key_of_conn[conn["id"]] = key
            self.name.setdefault(key, str(conn.get("name") or ""))
            self.registered.add(key)

    def resolve(self, fingerprint: str, host: str, fallback_name: str = "", pbs_id: int | None = None) -> str:
        # Resolved at sync time (DNS, certificate probe) when available.
        if pbs_id in self.key_of_conn:
            return self.key_of_conn[pbs_id]
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
    vms_by_storage: Mapping[str, int] | None = None,
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
            there = servers.resolve(
                job["remote_fingerprint"], job["remote_host"], job["remote_name"], job.get("remote_pbs_id")
            )
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
        node = (servers.resolve(t["fingerprint"], t["server"], pbs_id=t.get("pbs_id")), str(t["datastore"]))
        primary_edges.append({"dst": node, "dst_ns": str(t["namespace"] or ""), "storage_id": t["storage_id"]})
        reach(node, str(t["namespace"] or ""), 1)
    walk()

    # Datastores of this cluster's PBS that no edge reaches: still show their
    # capacity, unless the connection is limited to a namespace (then the
    # other stores belong to someone else).
    # Only for PBS that take no part in the flow at all: next to used
    # datastores, unrelated ones would just be noise.
    involved = {server for server, _store in depth}
    for conn in own_conns:
        key = servers.key_of_conn[conn["id"]]
        if conn.get("namespace") or key in involved:
            continue
        for server, store in sorted(usage_by_node):
            if server == key and (server, store) not in depth:
                reach((server, store), "", 1)
    walk()

    if not depth:
        return {"columns": [], "edges": [], "lines": []}

    vms_by_storage = vms_by_storage or {}
    primary_vms: dict[Node, int] = {}
    for p in primary_edges:
        p["vms"] = vms_by_storage.get(p["storage_id"])
        primary_vms[p["dst"]] = primary_vms.get(p["dst"], 0) + (p["vms"] or 0)
    max_vms = max((p["vms"] or 0 for p in primary_edges), default=0)

    layer, back_edges = _layers(depth, primary_edges, edges, primary_vms)

    own_conn_by_server = {servers.key_of_conn[c["id"]]: c for c in own_conns}
    node_ids = {node: f"ds-{i}" for i, node in enumerate(sorted(depth, key=lambda n: (layer[n], n)))}
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
            "primary": [],
            "incoming": [],
        }

    out_edges: list[dict[str, Any]] = []
    for p in primary_edges:
        edge = {
            "kind": "primary",
            "from": "cluster",
            "to": node_ids[p["dst"]],
            "to_ns": p["dst_ns"],
            "state": "ok",
            "storage_id": p["storage_id"],
            "vms": p["vms"],
            # A target only a few guests use (e.g. the PBS VM backing up
            # to another PBS) is drawn thinner than the main path.
            "minor": p["vms"] is not None and max_vms > 0 and p["vms"] <= max_vms / 4,
            "back": False,
        }
        out_edges.append(edge)
        nodes[p["dst"]]["primary"].append(edge)
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
            "minor": False,
            "back": id(e) in back_edges,
        }
        out_edges.append(edge)
        nodes[e["dst"]]["incoming"].append(edge)

    columns = _columns(layer, nodes, primary_vms, edges, primary_edges, servers, own_conn_by_server)
    for p, edge in zip(primary_edges, out_edges, strict=False):
        edge["span"] = layer[p["dst"]]
    for e, edge in zip(edges, out_edges[len(primary_edges) :], strict=True):
        edge["span"] = layer[e["dst"]] - layer[e["src"]]
    lines = [{k: e[k] for k in ("from", "to", "kind", "state", "back", "minor", "span")} for e in out_edges]
    return {"columns": columns, "edges": out_edges, "lines": lines}


def _layers(
    reached: Mapping[Node, int],
    primary_edges: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    primary_vms: Mapping[Node, int],
) -> tuple[dict[Node, int], set[int]]:
    """Column per datastore: longest path from the cluster, so a store that is
    both a (minor) primary target and a copy stands behind its source.
    Edges closing a cycle (mutual backup) are back edges and not counted."""
    out: dict[Node, list[dict[str, Any]]] = {}
    for e in edges:
        out.setdefault(e["src"], []).append(e)
    roots = sorted({p["dst"] for p in primary_edges}, key=lambda n: (-primary_vms.get(n, 0), n))
    roots += sorted(n for n in reached if n not in roots)

    back: set[int] = set()
    done: set[Node] = set()
    for root in roots:
        if root in done:
            continue
        on_path = {root}
        stack = [(root, iter(out.get(root, [])))]
        while stack:
            node, children = stack[-1]
            edge = next(children, None)
            if edge is None:
                stack.pop()
                on_path.discard(node)
                done.add(node)
                continue
            dst = edge["dst"]
            if dst in on_path:
                back.add(id(edge))
            elif dst not in done:
                on_path.add(dst)
                stack.append((dst, iter(out.get(dst, []))))

    layer = {node: 1 for node in reached}
    forward = [e for e in edges if id(e) not in back]
    for _ in range(len(reached)):  # longest path on the DAG (small graphs)
        changed = False
        for e in forward:
            if layer[e["dst"]] < layer[e["src"]] + 1:
                layer[e["dst"]] = layer[e["src"]] + 1
                changed = True
        if not changed:
            break
    return layer, back


def _columns(
    layer: Mapping[Node, int],
    nodes: Mapping[Node, dict[str, Any]],
    primary_vms: Mapping[Node, int],
    edges: list[dict[str, Any]],
    primary_edges: list[dict[str, Any]],
    servers: _Servers,
    own_conn_by_server: Mapping[str, Mapping[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Groups per (column, server), ordered by their predecessors' positions
    so lines cross as little as possible."""
    preds: dict[Node, list[Node]] = {}
    for e in edges:
        preds.setdefault(e["dst"], []).append(e["src"])
    position: dict[Node, float] = {}
    columns: list[list[dict[str, Any]]] = []
    for level in sorted(set(layer.values())):
        level_nodes = [n for n in layer if layer[n] == level]

        def weight(node: Node, first: bool = level == 1) -> float:
            before = [position[p] for p in preds.get(node, []) if p in position]
            if before:
                return sum(before) / len(before)
            # First column: the main backup target first.
            return -primary_vms.get(node, 0) if first else 1e9

        groups: dict[str, dict[str, Any]] = {}
        for node in sorted(level_nodes, key=lambda n: (weight(n), n)):
            group = groups.setdefault(
                node[0],
                {
                    "server": servers.name[node[0]],
                    "registered": node[0] in servers.registered,
                    "conn": own_conn_by_server.get(node[0]),
                    "stores": [],
                    "_weight": weight(node),
                },
            )
            group["stores"].append(nodes[node])
        ordered = sorted(groups.values(), key=lambda g: (g.pop("_weight"), g["server"]))
        index = 0
        for group in ordered:
            for store in group["stores"]:
                node = next(n for n in level_nodes if nodes[n] is store)
                position[node] = index
                index += 1
        columns.append(ordered)
    return columns
