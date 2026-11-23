from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.db_core import connect
from app.services.crypto import UndecryptableError, decrypt, encrypt, unwrap
from app.types import ClusterRow, NodeRow, PBSConnectionRow, VMRow

# How long an enrollment secret (the one in the onboarding commands) is accepted.
ENROLLMENT_VALID_HOURS = 24

# Stored encrypted (app.services.crypto); the rows handed out carry plaintext.
_CLUSTER_SECRETS = ("enrollment_secret", "token_secret")
_PBS_SECRETS = ("api_token_secret",)


def _open(row: Any, columns: tuple[str, ...]) -> Any:
    """Decrypt the secret columns of a row read from the database."""
    if row:
        for column in columns:
            if row.get(column):
                row[column] = decrypt(row[column])
    return row


def create_cluster(name: str, slug: str, api_url: str, enrollment_secret: str) -> None:
    now = datetime.now(UTC)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO clusters (name, slug, api_url, enrollment_secret, enrollment_expires_at, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                name,
                slug,
                api_url.rstrip("/"),
                encrypt(enrollment_secret),
                now + timedelta(hours=ENROLLMENT_VALID_HOURS),
                now,
            ),
        )
        conn.commit()


def renew_enrollment(cluster_id: int, enrollment_secret: str) -> None:
    """Replace the enrollment secret; the old one stops working at once."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE clusters SET enrollment_secret = %s, enrollment_expires_at = %s WHERE id = %s",
            (
                encrypt(enrollment_secret),
                datetime.now(UTC) + timedelta(hours=ENROLLMENT_VALID_HOURS),
                cluster_id,
            ),
        )
        conn.commit()


def enrollment_active(cluster: Any, now: datetime | None = None) -> bool:
    expires = cluster.get("enrollment_expires_at") if cluster else None
    return bool(expires and expires > (now or datetime.now(UTC)))


def enrollment_secret_matches(cluster: Any, presented: str, now: datetime | None = None) -> bool:
    """Whether `presented` is the cluster's enrollment secret and still valid."""
    import hmac

    stored = (cluster or {}).get("enrollment_secret") or ""
    same = hmac.compare_digest(stored.encode(), (presented or "").encode())
    return bool(stored) and same and enrollment_active(cluster, now)


# Secrets kept in app_settings (restore server, notifications).
_SETTING_SECRETS = ("restore_server.token_secret", "restore_server.ssh_private_key", "gotify_token", "email_password")


def encrypt_stored_secrets() -> tuple[int, list[str]]:
    """Bring every stored secret into its proper form: one layer, current key.

    Plaintext from older versions is encrypted; a value encrypted with a
    previous key (BSENTINEL_SECRET_KEY_OLD) — or wrapped several times by a
    start with a changed key — is opened and encrypted anew. A value that fits
    no known key is LEFT UNTOUCHED and reported: wrapping it again would only
    bury it deeper. Idempotent. Returns (values rewritten, unreadable values
    as "table.column#id")."""
    changed, unreadable = 0, []

    def proper(value: str, where: str) -> str:
        try:
            plain, as_it_should_be = unwrap(value)
        except UndecryptableError:
            unreadable.append(where)
            return value
        return value if as_it_should_be else encrypt(plain)

    with connect() as conn, conn.cursor() as cur:
        for table, columns in (("clusters", _CLUSTER_SECRETS), ("pbs_connections", _PBS_SECRETS)):
            cur.execute(f"SELECT id, {', '.join(columns)} FROM {table}")
            for row in cur.fetchall():
                for column in columns:
                    value = row[column]
                    if not value:
                        continue
                    sealed = proper(value, f"{table}.{column}#{row['id']}")
                    if sealed != value:
                        cur.execute(f"UPDATE {table} SET {column} = %s WHERE id = %s", (sealed, row["id"]))
                        changed += 1
        cur.execute("SELECT key, value FROM app_settings WHERE key = ANY(%s)", (list(_SETTING_SECRETS),))
        for row in cur.fetchall():
            if not row["value"]:
                continue
            sealed = proper(row["value"], f"app_settings.{row['key']}")
            if sealed != row["value"]:
                cur.execute("UPDATE app_settings SET value = %s WHERE key = %s", (sealed, row["key"]))
                changed += 1
        conn.commit()
    return changed, unreadable


def list_clusters() -> list[ClusterRow]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM clusters ORDER BY name")
        return [_open(row, _CLUSTER_SECRETS) for row in cur.fetchall()]


def get_cluster_by_slug(cluster_slug: str) -> ClusterRow | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM clusters WHERE slug = %s", (cluster_slug,))
        return _open(cur.fetchone(), _CLUSTER_SECRETS)


def set_cluster_report_since(cluster_id: int, month: date | None) -> None:
    """First month the NIS2 report measures this cluster in (None: since it was added)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE clusters SET report_since = %s WHERE id = %s", (month, cluster_id))
        conn.commit()


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
            WHERE slug = %s
            """,
            (
                payload["api_user"],
                payload["token_id"],
                encrypt(payload["token_secret"]),
                payload["fingerprint"],
                now,
                payload["cluster_slug"],
            ),
        )
        conn.commit()


def sync_cluster_inventory(cluster_id: int, nodes: list[dict[str, str]], vms: list[dict[str, Any]]) -> None:
    now = datetime.now(UTC)
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
                INSERT INTO vms (
                    cluster_id, node_name, vmid, name, guest_type, is_template, backup_storages, backup_kind, last_backup_at, memory_bytes, disk_bytes
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (cluster_id, vmid) DO UPDATE SET
                    node_name = EXCLUDED.node_name,
                    name = EXCLUDED.name,
                    guest_type = EXCLUDED.guest_type,
                    is_template = EXCLUDED.is_template,
                    backup_storages = EXCLUDED.backup_storages,
                    backup_kind = EXCLUDED.backup_kind,
                    last_backup_at = COALESCE(EXCLUDED.last_backup_at, vms.last_backup_at),
                    memory_bytes = COALESCE(EXCLUDED.memory_bytes, vms.memory_bytes),
                    disk_bytes = COALESCE(EXCLUDED.disk_bytes, vms.disk_bytes),
                    missing_since = NULL
                RETURNING id
                """,
                (
                    cluster_id,
                    vm["node_name"],
                    vm["vmid"],
                    vm["name"],
                    vm.get("guest_type", "qemu"),
                    bool(vm.get("is_template")),
                    vm.get("backup_storages", []),
                    vm["backup_kind"],
                    vm["last_backup_at"],
                    vm.get("memory_bytes"),
                    vm.get("disk_bytes"),
                ),
            )
            vm_id = cur.fetchone()["id"]
            # Replace the guest's per-disk breakdown. Only touch it when the sync
            # actually read a config (an empty list may just mean the config
            # fetch failed) so a transient PVE hiccup doesn't wipe known disks.
            disks = vm.get("disks") or []
            if disks:
                cur.execute("DELETE FROM vm_disks WHERE vm_id = %s", (vm_id,))
                for ordinal, disk in enumerate(disks):
                    cur.execute(
                        "INSERT INTO vm_disks (vm_id, ordinal, dev, storage, size_bytes) VALUES (%s, %s, %s, %s, %s)",
                        (vm_id, ordinal, disk["dev"], disk["storage"], int(disk["size_bytes"])),
                    )
            else:
                # Config not read this time: disk_bytes fell back to PVE's
                # maxdisk, which is only the boot disk. Keep the size in step
                # with the known breakdown (sum of all volumes).
                cur.execute(
                    """
                    UPDATE vms SET disk_bytes = d.total
                    FROM (SELECT SUM(size_bytes) AS total FROM vm_disks WHERE vm_id = %s) d
                    WHERE vms.id = %s AND d.total > 0
                    """,
                    (vm_id, vm_id),
                )
        # VMs we still know but PVE no longer reports have been deleted (or
        # moved to another cluster). Keep the rows so their remaining backups
        # stay visible, but flag them from the first sync they went missing.
        cur.execute(
            """
            UPDATE vms SET missing_since = COALESCE(missing_since, %s)
            WHERE cluster_id = %s AND NOT (vmid = ANY(%s))
            """,
            (now, cluster_id, [vm["vmid"] for vm in vms]),
        )
        conn.commit()


def prune_gone_vms_without_backups(cluster_id: int) -> int:
    """Delete VMs gone from PVE that have no real backup left.

    Cascades to policies, restore tests and backup events. Callers must only
    run this after a sync where every PVE/PBS source was fully readable.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM vms
            WHERE cluster_id = %s
              AND missing_since IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM backup_events be
                  WHERE be.vm_id = vms.id AND be.status = 'ok' AND be.removed_at IS NULL
                    AND COALESCE(be.size_bytes, 0) > 1
              )
            """,
            (cluster_id,),
        )
        deleted = cur.rowcount
        conn.commit()
        return deleted


def list_known_vmids(cluster_id: int) -> set[int]:
    """All VMIDs ever seen in this cluster, including ones no longer in PVE."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT vmid FROM vms WHERE cluster_id = %s", (cluster_id,))
        return {row["vmid"] for row in cur.fetchall()}


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
        return _open(dict(row), _CLUSTER_SECRETS) if row else None


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
        return [_open(row, _CLUSTER_SECRETS) for row in cur.fetchall()]


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


def node_names_by_cluster() -> dict[int, list[str]]:
    """Node names of every cluster in one query."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT cluster_id, name FROM nodes ORDER BY cluster_id, name")
        result: dict[int, list[str]] = {}
        for row in cur.fetchall():
            result.setdefault(row["cluster_id"], []).append(row["name"])
        return result


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
                backup_policy_override = EXCLUDED.backup_policy_override
            """,
            (vm_id, backup_policy_override, True),
        )
        conn.commit()


def upsert_pbs_connection(cluster_id: int, payload: dict[str, Any]) -> None:
    now = datetime.now(UTC)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pbs_connections
                (cluster_id, name, api_url, api_token_id, api_token_secret, fingerprint, namespace, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, ''), %s)
            ON CONFLICT (cluster_id, api_url) DO UPDATE SET
                name = EXCLUDED.name,
                api_token_id = EXCLUDED.api_token_id,
                api_token_secret = EXCLUDED.api_token_secret,
                fingerprint = EXCLUDED.fingerprint,
                -- A re-run without --namespace keeps the configured one.
                namespace = COALESCE(%s, pbs_connections.namespace)
            """,
            (
                cluster_id,
                payload["name"],
                payload["api_url"].rstrip("/"),
                payload["api_token_id"],
                encrypt(payload["api_token_secret"]),
                payload.get("fingerprint", ""),
                payload.get("namespace"),
                now,
                payload.get("namespace"),
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
        return [_open(row, _PBS_SECRETS) for row in cur.fetchall()]


def normalize_namespace(namespace: str) -> str:
    """PBS namespace path without surrounding or doubled slashes ("/a//b/" → "a/b")."""
    return "/".join(part for part in namespace.strip().split("/") if part)


def normalize_aliases(aliases: str) -> list[str]:
    """Host names/IPs separated by commas, spaces or lines; lower case, unique."""
    return list(dict.fromkeys(a.lower() for a in re.split(r"[\s,;]+", aliases) if a))


def update_pbs_settings(cluster_id: int, conn_id: int, namespace: str, aliases: str) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE pbs_connections SET namespace = %s, aliases = %s WHERE id = %s AND cluster_id = %s",
            (normalize_namespace(namespace), normalize_aliases(aliases), conn_id, cluster_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
        return updated


def delete_pbs_connection(conn_id: int) -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM pbs_connections WHERE id = %s", (conn_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted


def replace_datastore_usage(pbs_id: int, rows: list[dict[str, Any]]) -> None:
    """Store the latest capacity of a PBS's datastores (removed stores disappear)."""
    now = datetime.now(UTC)
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM pbs_datastore_usage WHERE pbs_id = %s", (pbs_id,))
        for row in rows:
            cur.execute(
                """
                INSERT INTO pbs_datastore_usage
                    (pbs_id, store, total_bytes, used_bytes, avail_bytes, estimated_full_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    pbs_id,
                    row["store"],
                    row["total_bytes"],
                    row["used_bytes"],
                    row["avail_bytes"],
                    row["estimated_full_at"],
                    now,
                ),
            )
        conn.commit()


def list_datastore_usage(cluster_id: int | None = None) -> list[dict[str, Any]]:
    query = """
        SELECT u.pbs_id, u.store, u.total_bytes, u.used_bytes, u.avail_bytes, u.updated_at,
               -- A full date in the past is no forecast (see fetch_datastore_usage).
               CASE WHEN u.estimated_full_at > NOW() THEN u.estimated_full_at END AS estimated_full_at,
               p.name AS pbs_name, p.cluster_id, p.fingerprint AS pbs_fingerprint
        FROM pbs_datastore_usage u
        JOIN pbs_connections p ON p.id = u.pbs_id
    """
    params: tuple[Any, ...] = ()
    if cluster_id is not None:
        query += " WHERE p.cluster_id = %s"
        params = (cluster_id,)
    query += " ORDER BY p.name, u.store"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


def replace_pve_pbs_targets(cluster_id: int, targets: list[dict[str, str]]) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM pve_pbs_targets WHERE cluster_id = %s", (cluster_id,))
        cur.executemany(
            """
            INSERT INTO pve_pbs_targets (cluster_id, storage_id, server, fingerprint, datastore, namespace, pbs_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            """,
            [
                (
                    cluster_id,
                    t["storage_id"],
                    t["server"],
                    t["fingerprint"],
                    t["datastore"],
                    t["namespace"],
                    t.get("pbs_id"),
                )
                for t in targets
            ],
        )
        conn.commit()


def list_pve_pbs_targets(cluster_id: int) -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM pve_pbs_targets WHERE cluster_id = %s ORDER BY storage_id", (cluster_id,))
        return list(cur.fetchall())


_SYNC_JOB_COLUMNS = (
    "job_id",
    "direction",
    "store",
    "ns",
    "remote_name",
    "remote_host",
    "remote_fingerprint",
    "remote_store",
    "remote_ns",
    "schedule",
    "last_run_state",
    "last_run_at",
    "next_run_at",
    "remote_pbs_id",
    "running_since",
    "running_upid",
)


def replace_pbs_sync_jobs(pbs_id: int, jobs: list[dict[str, Any]]) -> None:
    columns = ", ".join(_SYNC_JOB_COLUMNS)
    placeholders = ", ".join(["%s"] * (len(_SYNC_JOB_COLUMNS) + 1))
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM pbs_sync_jobs WHERE pbs_id = %s", (pbs_id,))
        cur.executemany(
            f"INSERT INTO pbs_sync_jobs (pbs_id, {columns}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
            [(pbs_id, *(job.get(c) for c in _SYNC_JOB_COLUMNS)) for job in jobs],
        )
        conn.commit()


def list_pbs_sync_jobs() -> list[dict[str, Any]]:
    """Sync jobs of all registered PBS — replication crosses cluster boundaries."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM pbs_sync_jobs ORDER BY pbs_id, job_id")
        return list(cur.fetchall())


def update_pbs_cert_fingerprints(pbs_id: int, fingerprints: list[str]) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE pbs_connections SET cert_fingerprints = %s WHERE id = %s", (fingerprints, pbs_id))
        conn.commit()


def save_cluster_topology(cluster_id: int, data_json: str) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cluster_topology (cluster_id, data, built_at) VALUES (%s, %s::jsonb, %s)
            ON CONFLICT (cluster_id) DO UPDATE SET data = EXCLUDED.data, built_at = EXCLUDED.built_at
            """,
            (cluster_id, data_json, datetime.now(UTC)),
        )
        conn.commit()


def get_cluster_topology(cluster_id: int) -> dict[str, Any] | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT data FROM cluster_topology WHERE cluster_id = %s", (cluster_id,))
        row = cur.fetchone()
        return row["data"] if row else None


def job_storages_by_vmid(cluster_id: int) -> dict[int, list[str]]:
    """Storages each existing guest is backed up to directly by its PVE backup
    jobs (a PBS sync's copies in the same datastore do not count)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT vmid, backup_storages FROM vms
            WHERE cluster_id = %s AND missing_since IS NULL AND cardinality(backup_storages) > 0
            """,
            (cluster_id,),
        )
        return {row["vmid"]: list(row["backup_storages"]) for row in cur.fetchall()}


def update_cluster_qdevice(cluster_id: int, qdevice: dict[str, str]) -> None:
    """Store the cluster's quorum device ({} = none)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE clusters SET qdevice = %s::jsonb WHERE id = %s",
            (json.dumps(qdevice) if qdevice else None, cluster_id),
        )
        conn.commit()


def update_vm_ha(cluster_id: int, ha_by_vmid: dict[int, dict[str, Any]]) -> None:
    """Store which guests PVE HA manages (and where they may run); others NULL."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE vms SET ha = NULL WHERE cluster_id = %s AND ha IS NOT NULL", (cluster_id,))
        cur.executemany(
            "UPDATE vms SET ha = %s::jsonb WHERE cluster_id = %s AND vmid = %s",
            [(json.dumps(info), cluster_id, vmid) for vmid, info in ha_by_vmid.items()],
        )
        conn.commit()


def present_guests_by_store(cluster_id: int) -> dict[tuple[str, str], dict[int, datetime]]:
    """(server, datastore) → guests of the cluster with backups lying there
    now, each with its latest one there, from the locations the sync
    recorded ("pbs:server/store[/ns]")."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.vmid, loc, MAX(be.started_at) AS latest
            FROM backup_events be
            JOIN vms v ON v.id = be.vm_id
            CROSS JOIN LATERAL unnest(be.locations) AS loc
            WHERE v.cluster_id = %s AND be.removed_at IS NULL AND be.status = 'ok'
              AND COALESCE(be.size_bytes, 0) > 1 AND loc LIKE 'pbs:%%'
            GROUP BY v.vmid, loc
            """,
            (cluster_id,),
        )
        result: dict[tuple[str, str], dict[int, datetime]] = {}
        for row in cur.fetchall():
            parts = row["loc"][len("pbs:") :].split("/")
            if len(parts) >= 2:
                guests = result.setdefault((parts[0], parts[1]), {})
                guests[row["vmid"]] = max(guests.get(row["vmid"], row["latest"]), row["latest"])
        return result
