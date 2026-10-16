"""Precomputed replication diagrams, so the cluster page only reads one row.

Built at the end of every sync (for all clusters: a PBS and its sync jobs can
be shared between clusters) and after PBS settings change.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from app import db
from app.services.pbs_topology import build_topology

logger = logging.getLogger(__name__)

_DATE_KEYS = {"estimated_full_at", "last_run_at", "next_run_at"}


def build(cluster_id: int) -> dict[str, Any]:
    return build_topology(
        cluster_id,
        db.list_pve_pbs_targets(cluster_id),
        db.list_pbs_connections(),
        db.list_pbs_sync_jobs(),
        db.list_datastore_usage(),
        vms_by_storage=db.vm_count_by_storage(cluster_id),
    )


def _encode(topology: dict[str, Any]) -> str:
    def default(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(f"not serialisable: {type(value).__name__}")

    return json.dumps(topology, default=default)


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: datetime.fromisoformat(v) if k in _DATE_KEYS and isinstance(v, str) else _decode(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def refresh_all() -> None:
    """Rebuild every cluster's diagram (best effort)."""
    for cluster in db.list_clusters():
        try:
            db.save_cluster_topology(cluster["id"], _encode(build(cluster["id"])))
        except Exception:
            logger.exception("Building the topology of cluster %s failed", cluster.get("name"))


def load(cluster_id: int) -> dict[str, Any]:
    """The stored diagram; built once on the spot if there is none yet."""
    stored = db.get_cluster_topology(cluster_id)
    if stored is not None:
        return _decode(stored)
    topology = build(cluster_id)
    db.save_cluster_topology(cluster_id, _encode(topology))
    return topology
