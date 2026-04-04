from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.config import SYNC_INTERVAL_MINUTES
from app.db_core import connect
from app.types import ClusterRow, NodeRow, PBSConnectionRow, SyncHealthRow, VMRow


def create_cluster(name: str, slug: str, api_url: str, enrollment_secret: str) -> None:
    now = datetime.now(UTC)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO clusters (name, slug, api_url, enrollment_secret, created_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (name, slug, api_url.rstrip("/"), enrollment_secret, now),
        )
        conn.commit()


def list_clusters() -> list[ClusterRow]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM clusters ORDER BY name")
        return list(cur.fetchall())


def get_cluster_by_slug(cluster_slug: str) -> ClusterRow | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM clusters WHERE slug = %s", (cluster_slug,))
        return cur.fetchone()


def delete_cluster(cluster_slug: str) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM clusters WHERE slug = %s", (cluster_slug,))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted


def finalize_cluster_registration(payload: dict[str, str]) -> None:
    now = datetime.now(UTC)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE clusters
            SET api_user = %s, token_id = %s, token_secret = %s, fingerprint = %s, registered_at = %s
            WHERE slug = %s AND enrollment_secret = %s
            """,
            (
                payload["api_user"],
                payload["token_id"],
                payload["token_secret"],
                payload["fingerprint"],
                now,
                payload["cluster_slug"],
                payload["enrollment_secret"],
            ),
        )
        conn.commit()


def sync_cluster_inventory(cluster_id: int, nodes: list[dict[str, str]], vms: list[dict[str, Any]]) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM nodes WHERE cluster_id = %s", (cluster_id,))
        for node in nodes:
            cur.execute(
                "INSERT INTO nodes (cluster_id, name, status) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (cluster_id, node["name"], node["status"]),
            )
        for vm in vms:
            cur.execute(
                """
                INSERT INTO vms (cluster_id, node_name, vmid, name, backup_kind, last_backup_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (cluster_id, vmid) DO UPDATE SET
                    node_name = EXCLUDED.node_name,
                    name = EXCLUDED.name,
                    backup_kind = EXCLUDED.backup_kind,
                    last_backup_at = COALESCE(EXCLUDED.last_backup_at, vms.last_backup_at)
                """,
                (cluster_id, vm["node_name"], vm["vmid"], vm["name"], vm["backup_kind"], vm["last_backup_at"]),
            )
        conn.commit()


def update_vm_last_backup_from_events(cluster_id: int) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE vms SET last_backup_at = NULL WHERE cluster_id = %s", (cluster_id,))
        cur.execute(
            """
            UPDATE vms v SET last_backup_at = be.started_at
            FROM (
                SELECT vm_id, MAX(started_at) AS started_at
                FROM backup_events be
                JOIN vms vv ON vv.id = be.vm_id
                WHERE vv.cluster_id = %s
                  AND be.status = 'ok'
                  AND be.removed_at IS NULL
                  AND COALESCE(be.size_bytes, 0) > 1
                GROUP BY vm_id
            ) be
            WHERE v.id = be.vm_id
            """,
            (cluster_id,),
        )
        conn.commit()


def rename_cluster(cluster_id: int, new_name: str, new_slug: str) -> ClusterRow | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE clusters SET name = %s, slug = %s WHERE id = %s RETURNING *",
            (new_name, new_slug, cluster_id),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None


def update_cluster_sync_status(cluster_id: int, ok: bool, log: str) -> None:
    now = datetime.now(UTC)
    with connect() as conn, conn.cursor() as cur:
        if ok:
            cur.execute(
                """
                UPDATE clusters
                SET last_pve_sync_at = %s,
                    last_pve_sync_ok = %s,
                    last_pve_sync_log = %s,
                    last_pve_sync_success_at = %s,
                    last_pve_sync_failure_started_at = NULL,
                    last_pve_sync_failure_notified_at = NULL,
                    consecutive_sync_failures = 0
                WHERE id = %s
                """,
                (now, ok, log, now, cluster_id),
            )
        else:
            cur.execute(
                """
                UPDATE clusters
                SET last_pve_sync_at = %s,
                    last_pve_sync_ok = %s,
                    last_pve_sync_log = %s,
                    last_pve_sync_failure_started_at = COALESCE(last_pve_sync_failure_started_at, %s),
                    consecutive_sync_failures = consecutive_sync_failures + 1
                WHERE id = %s
                """,
                (now, ok, log, now, cluster_id),
            )
        conn.commit()


def list_clusters_with_sync_overdue(threshold_hours: int = 24) -> list[ClusterRow]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM clusters
            WHERE last_pve_sync_ok = FALSE
              AND last_pve_sync_failure_started_at IS NOT NULL
              AND last_pve_sync_failure_started_at <= NOW() - (%s * INTERVAL '1 hour')
              AND (
                  last_pve_sync_failure_notified_at IS NULL
                  OR last_pve_sync_failure_notified_at < last_pve_sync_failure_started_at
              )
            ORDER BY name
            """,
            (threshold_hours,),
        )
        return list(cur.fetchall())


def mark_cluster_sync_overdue_notified(cluster_id: int) -> None:
    now = datetime.now(UTC)
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE clusters SET last_pve_sync_failure_notified_at = %s WHERE id = %s", (now, cluster_id))
        conn.commit()


def update_pbs_sync_status(pbs_id: int, ok: bool, log: str) -> None:
    now = datetime.now(UTC)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE pbs_connections SET last_sync_at = %s, last_sync_ok = %s, last_sync_log = %s WHERE id = %s",
            (now, ok, log, pbs_id),
        )
        conn.commit()


def list_nodes(cluster_id: int) -> list[NodeRow]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM nodes WHERE cluster_id = %s ORDER BY name", (cluster_id,))
        return list(cur.fetchall())


def list_vms(cluster_id: int | None = None) -> list[VMRow]:
    query = "SELECT * FROM vms"
    params: tuple[Any, ...] = ()
    if cluster_id is not None:
        query += " WHERE cluster_id = %s"
        params = (cluster_id,)
    query += " ORDER BY backup_kind, name"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


def get_vm_for_cluster(cluster_id: int, vm_id: int) -> VMRow | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM vms WHERE id = %s AND cluster_id = %s", (vm_id, cluster_id))
        return cur.fetchone()


def update_vm_backup_policy(vm_id: int, backup_policy_override: str | None) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO vm_policies (vm_id, owner_name, criticality, backup_policy_override, restore_interval_days, restore_required, rpo_hours, notes)
            VALUES (%s, 'unassigned', 'medium', %s, NULL, %s, 24, 'Backup policy override')
            ON CONFLICT (vm_id) DO UPDATE SET
                backup_policy_override = EXCLUDED.backup_policy_override,
                restore_required = EXCLUDED.restore_required
            """,
            (vm_id, backup_policy_override, backup_policy_override != "none"),
        )
        conn.commit()


def upsert_pbs_connection(cluster_id: int, payload: dict[str, Any]) -> None:
    now = datetime.now(UTC)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pbs_connections
                (cluster_id, name, api_url, api_token_id, api_token_secret, fingerprint, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (cluster_id, api_url) DO UPDATE SET
                name = EXCLUDED.name,
                api_token_id = EXCLUDED.api_token_id,
                api_token_secret = EXCLUDED.api_token_secret,
                fingerprint = EXCLUDED.fingerprint
            """,
            (
                cluster_id, payload["name"], payload["api_url"].rstrip("/"),
                payload["api_token_id"], payload["api_token_secret"], payload.get("fingerprint", ""), now,
            ),
        )
        conn.commit()


def list_pbs_connections(cluster_id: int | None = None) -> list[PBSConnectionRow]:
    query = "SELECT * FROM pbs_connections"
    params: tuple[Any, ...] = ()
    if cluster_id is not None:
        query += " WHERE cluster_id = %s"
        params = (cluster_id,)
    query += " ORDER BY created_at ASC"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


def delete_pbs_connection(conn_id: int) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM pbs_connections WHERE id = %s", (conn_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted


def get_cluster_sync_health(cluster_id: int) -> SyncHealthRow:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM clusters WHERE id = %s", (cluster_id,))
        cluster = cur.fetchone()
        if not cluster:
            return {"failures": 0, "stale": False}
        if cluster.get("last_pve_sync_ok") is not False:
            return {"failures": 0, "stale": False}
        failure_start = cluster.get("last_pve_sync_failure_started_at")
        if not failure_start:
            return {"failures": 1, "stale": False}
        delta_hours = (datetime.now(UTC) - failure_start).total_seconds() / 3600
        est_failures = max(1, int(delta_hours / (SYNC_INTERVAL_MINUTES / 60)))
        return {"failures": est_failures, "stale": est_failures >= 3}
