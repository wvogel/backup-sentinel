from __future__ import annotations

from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import DATABASE_URL, REPORT_DIR

_pool: ConnectionPool | None = None


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            DATABASE_URL,
            kwargs={"row_factory": dict_row},
            min_size=1,
            max_size=10,
            open=True,
        )
    return _pool


def connect() -> ConnectionPool.Connection:  # type: ignore[name-defined]
    return _get_pool().connection()


def init_db() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True, mode=0o750)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS clusters (
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    slug TEXT NOT NULL UNIQUE,
                    api_url TEXT NOT NULL,
                    enrollment_secret TEXT NOT NULL,
                    api_user TEXT,
                    token_id TEXT,
                    token_secret TEXT,
                    fingerprint TEXT,
                    registered_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL
                );
                """
            )
            cur.execute("ALTER TABLE clusters DROP COLUMN IF EXISTS notes")
            cur.execute("ALTER TABLE clusters ADD COLUMN IF NOT EXISTS last_pve_sync_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE clusters ADD COLUMN IF NOT EXISTS last_pve_sync_ok BOOLEAN")
            cur.execute("ALTER TABLE clusters ADD COLUMN IF NOT EXISTS last_pve_sync_log TEXT")
            cur.execute("ALTER TABLE clusters ADD COLUMN IF NOT EXISTS last_pve_sync_success_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE clusters ADD COLUMN IF NOT EXISTS last_pve_sync_failure_started_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE clusters ADD COLUMN IF NOT EXISTS last_pve_sync_failure_notified_at TIMESTAMPTZ")
            cur.execute(
                "ALTER TABLE clusters ADD COLUMN IF NOT EXISTS consecutive_sync_failures INTEGER NOT NULL DEFAULT 0"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS nodes (
                    id BIGSERIAL PRIMARY KEY,
                    cluster_id BIGINT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    UNIQUE(cluster_id, name)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS vms (
                    id BIGSERIAL PRIMARY KEY,
                    cluster_id BIGINT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
                    node_name TEXT NOT NULL,
                    vmid INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    backup_kind TEXT NOT NULL,
                    last_backup_at TIMESTAMPTZ,
                    UNIQUE(cluster_id, vmid)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS monthly_reports (
                    id BIGSERIAL PRIMARY KEY,
                    report_month DATE NOT NULL,
                    generated_at TIMESTAMPTZ NOT NULL,
                    trigger_mode TEXT NOT NULL,
                    pdf_path TEXT NOT NULL,
                    json_path TEXT NOT NULL,
                    snapshot_json JSONB NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS vm_policies (
                    vm_id BIGINT PRIMARY KEY REFERENCES vms(id) ON DELETE CASCADE,
                    owner_name TEXT NOT NULL,
                    criticality TEXT NOT NULL,
                    backup_policy_override TEXT,
                    restore_interval_days INTEGER,
                    restore_required BOOLEAN NOT NULL DEFAULT TRUE,
                    rpo_hours INTEGER NOT NULL,
                    notes TEXT NOT NULL DEFAULT ''
                );
                """
            )
            cur.execute("ALTER TABLE vm_policies ADD COLUMN IF NOT EXISTS backup_policy_override TEXT")
            cur.execute("ALTER TABLE vm_policies ALTER COLUMN restore_interval_days DROP NOT NULL")
            cur.execute("UPDATE vm_policies SET restore_interval_days = NULL WHERE restore_interval_days = 90")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS restore_tests (
                    id BIGSERIAL PRIMARY KEY,
                    vm_id BIGINT NOT NULL REFERENCES vms(id) ON DELETE CASCADE,
                    tested_at DATE NOT NULL,
                    result TEXT NOT NULL,
                    recovery_type TEXT NOT NULL,
                    duration_minutes INTEGER NOT NULL,
                    evidence_note TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS backup_events (
                    id BIGSERIAL PRIMARY KEY,
                    vm_id BIGINT NOT NULL REFERENCES vms(id) ON DELETE CASCADE,
                    started_at TIMESTAMPTZ NOT NULL,
                    finished_at TIMESTAMPTZ,
                    duration_seconds INTEGER,
                    size_bytes BIGINT,
                    encrypted BOOLEAN,
                    verify_state TEXT,
                    status TEXT NOT NULL DEFAULT 'ok',
                    upid TEXT,
                    source TEXT,
                    removed_at DATE,
                    UNIQUE(vm_id, started_at)
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_backup_events_started_at ON backup_events(started_at)")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_backup_events_vm_status ON backup_events(vm_id, status, removed_at)"
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vms_cluster_vmid ON vms(cluster_id, vmid)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_nodes_cluster ON nodes(cluster_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_clusters_slug ON clusters(slug)")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pbs_connections (
                    id BIGSERIAL PRIMARY KEY,
                    cluster_id BIGINT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    api_url TEXT NOT NULL,
                    api_token_id TEXT NOT NULL,
                    api_token_secret TEXT NOT NULL,
                    fingerprint TEXT NOT NULL DEFAULT '',
                    last_sync_at TIMESTAMPTZ,
                    last_sync_ok BOOLEAN,
                    last_sync_log TEXT,
                    created_at TIMESTAMPTZ NOT NULL,
                    UNIQUE(cluster_id, api_url)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS settings_audit_log (
                    id BIGSERIAL PRIMARY KEY,
                    actor TEXT NOT NULL,
                    key TEXT NOT NULL,
                    old_value TEXT NOT NULL,
                    new_value TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_log (
                    id BIGSERIAL PRIMARY KEY,
                    notification_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    cluster_name TEXT,
                    channel TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_notification_log_created_at ON notification_log(created_at)")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_dedup (
                    dedup_key TEXT PRIMARY KEY,
                    sent_at TIMESTAMPTZ NOT NULL
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_notification_dedup_sent_at ON notification_dedup(sent_at)")

            cur.execute("UPDATE clusters SET api_url = RTRIM(api_url, '/') WHERE api_url LIKE '%/'")
            cur.execute("UPDATE pbs_connections SET api_url = RTRIM(api_url, '/') WHERE api_url LIKE '%/'")
        conn.commit()


def bootstrap_script() -> str:
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "proxmox-bootstrap.sh"
    return script_path.read_text(encoding="utf-8")


def pbs_bootstrap_script() -> str:
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "pbs-bootstrap.sh"
    return script_path.read_text(encoding="utf-8")
