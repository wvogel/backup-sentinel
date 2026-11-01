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
            # 2 warm connections cover steady-state traffic; scale up to 15
            # under concurrent load (auto-sync + Prometheus scrape + UI).
            min_size=2,
            max_size=15,
            # Give up after 10s rather than hanging a request forever.
            timeout=10.0,
            # Recycle idle connections after 5 minutes to avoid stale TCP.
            max_idle=300.0,
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
            # Corosync quorum device ({state, host, model, algorithm}); NULL = none.
            cur.execute("ALTER TABLE clusters ADD COLUMN IF NOT EXISTS qdevice JSONB")
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
            cur.execute("ALTER TABLE vms ADD COLUMN IF NOT EXISTS missing_since TIMESTAMPTZ")
            cur.execute("ALTER TABLE vms ADD COLUMN IF NOT EXISTS guest_type TEXT NOT NULL DEFAULT 'qemu'")
            # PVE HA: {state, placement, nodes}; NULL = not HA-managed.
            cur.execute("ALTER TABLE vms ADD COLUMN IF NOT EXISTS ha JSONB")
            cur.execute("ALTER TABLE vms ADD COLUMN IF NOT EXISTS is_template BOOLEAN NOT NULL DEFAULT FALSE")
            # Configured RAM (maxmem) in bytes — the restore scheduler needs it to
            # decide whether a restored guest fits the host's free RAM.
            cur.execute("ALTER TABLE vms ADD COLUMN IF NOT EXISTS memory_bytes BIGINT")
            # Storages the guest's PVE backup jobs write to (direct backups).
            cur.execute("ALTER TABLE vms ADD COLUMN IF NOT EXISTS backup_storages TEXT[] NOT NULL DEFAULT '{}'")
            # PBS storages in the cluster's PVE config: where PVE backs up to.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pve_pbs_targets (
                    cluster_id BIGINT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
                    storage_id TEXT NOT NULL,
                    server TEXT NOT NULL,
                    fingerprint TEXT NOT NULL DEFAULT '',
                    datastore TEXT NOT NULL,
                    namespace TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (cluster_id, storage_id)
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
            # Automated restore testing (see docs/.../automated-restore-testing-design.md §8).
            cur.execute(
                "ALTER TABLE vm_policies ADD COLUMN IF NOT EXISTS auto_restore_enabled BOOLEAN NOT NULL DEFAULT FALSE"
            )
            cur.execute(
                "ALTER TABLE vm_policies ADD COLUMN IF NOT EXISTS restore_cadence TEXT NOT NULL DEFAULT 'half_year'"
            )
            cur.execute("ALTER TABLE vm_policies ADD COLUMN IF NOT EXISTS probe_primary_ip TEXT")
            # probe_primary_ip is now an IP or IP/prefix (CIDR) — networks are not
            # always /24. Existing bare-IP overrides mean a /24, so make that explicit.
            cur.execute(
                "UPDATE vm_policies SET probe_primary_ip = probe_primary_ip || '/24' "
                "WHERE probe_primary_ip IS NOT NULL AND probe_primary_ip <> '' "
                "AND probe_primary_ip NOT LIKE '%/%'"
            )
            # Optional VLAN the guest tags internally (its IP is on a VLAN
            # sub-interface): trunk it through to the sandbox so it stays reachable.
            cur.execute("ALTER TABLE vm_policies ADD COLUMN IF NOT EXISTS probe_vlan INTEGER")
            cur.execute(
                "ALTER TABLE vm_policies ADD COLUMN IF NOT EXISTS probe_tcp_ports INTEGER[] NOT NULL DEFAULT '{}'"
            )
            cur.execute(
                "ALTER TABLE vm_policies ADD COLUMN IF NOT EXISTS probe_require_icmp BOOLEAN NOT NULL DEFAULT TRUE"
            )
            cur.execute(
                "ALTER TABLE vm_policies ADD COLUMN IF NOT EXISTS hold_on_failure BOOLEAN NOT NULL DEFAULT FALSE"
            )
            cur.execute(
                "ALTER TABLE vm_policies ADD COLUMN IF NOT EXISTS restore_retry_days INTEGER NOT NULL DEFAULT 7"
            )
            cur.execute("ALTER TABLE vm_policies ADD COLUMN IF NOT EXISTS next_run_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE vm_policies ADD COLUMN IF NOT EXISTS probe_window_seconds INTEGER")
            cur.execute("ALTER TABLE vm_policies ADD COLUMN IF NOT EXISTS probe_first_delay_seconds INTEGER")
            cur.execute("ALTER TABLE vm_policies ADD COLUMN IF NOT EXISTS probe_interval_seconds INTEGER")
            cur.execute("ALTER TABLE vm_policies ADD COLUMN IF NOT EXISTS stall_backstop_factor REAL")
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
            # Automated restore testing: operational record per run (spec §14).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS restore_runs (
                    id BIGSERIAL PRIMARY KEY,
                    vm_id BIGINT NOT NULL REFERENCES vms(id) ON DELETE CASCADE,
                    trigger TEXT NOT NULL DEFAULT 'scheduled',
                    status TEXT NOT NULL DEFAULT 'queued',
                    restore_vmid INTEGER,
                    helper_vmid INTEGER,
                    vlan_tag INTEGER,
                    mgmt_ip TEXT,
                    source_pbs TEXT,
                    source_datastore TEXT,
                    source_namespace TEXT,
                    snapshot_id TEXT,
                    target_storages TEXT[] NOT NULL DEFAULT '{}',
                    bytes_total BIGINT,
                    bytes_done BIGINT,
                    percent SMALLINT,
                    throughput_bps BIGINT,
                    eta TIMESTAMPTZ,
                    result TEXT,
                    restore_test_id BIGINT REFERENCES restore_tests(id) ON DELETE SET NULL,
                    log TEXT NOT NULL DEFAULT '',
                    checks JSONB,
                    queued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    started_at TIMESTAMPTZ,
                    finished_at TIMESTAMPTZ,
                    duration_seconds INTEGER
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_restore_runs_status ON restore_runs(status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_restore_runs_vm ON restore_runs(vm_id, queued_at DESC)")
            # PVE restore-task UPID: lets a restarted app re-attach to a running
            # restore instead of orphaning (and reaping) it — a deploy must not
            # interrupt an in-flight restore.
            cur.execute("ALTER TABLE restore_runs ADD COLUMN IF NOT EXISTS task_upid TEXT")
            # Current pipeline step of a running run (restoring/network/helper/
            # booting/probing), for the live progress card.
            cur.execute("ALTER TABLE restore_runs ADD COLUMN IF NOT EXISTS phase TEXT")
            # The restored guest's IP as discovered at probe time (override,
            # recorded, or guest agent), shown read-only in the VM's settings.
            cur.execute("ALTER TABLE restore_runs ADD COLUMN IF NOT EXISTS detected_ip TEXT")
            # Operator asked to abort a running run; the worker's long loops poll
            # this and tear the environment down cleanly (status → cancelled).
            cur.execute(
                "ALTER TABLE restore_runs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE"
            )
            # Guest network interfaces collected from production (spec §9).
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS vm_network_interfaces (
                    vm_id BIGINT NOT NULL REFERENCES vms(id) ON DELETE CASCADE,
                    nic_index INTEGER NOT NULL,
                    mac TEXT NOT NULL,
                    ip_addresses TEXT[] NOT NULL DEFAULT '{}',
                    prefix INTEGER,
                    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
                    source TEXT NOT NULL DEFAULT '',
                    seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (vm_id, nic_index)
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
            cur.execute("ALTER TABLE backup_events ADD COLUMN IF NOT EXISTS storages TEXT[]")
            cur.execute("ALTER TABLE backup_events ADD COLUMN IF NOT EXISTS fetch_origin TEXT")
            # Physical locations (see app.services.backup_location) for the second-copy check.
            cur.execute("ALTER TABLE backup_events ADD COLUMN IF NOT EXISTS locations TEXT[]")
            # Details from finished vzdump task logs (see app.services.vzdump_log).
            cur.execute("ALTER TABLE backup_events ADD COLUMN IF NOT EXISTS transferred_bytes BIGINT")
            cur.execute("ALTER TABLE backup_events ADD COLUMN IF NOT EXISTS throughput_bps BIGINT")
            cur.execute("ALTER TABLE backup_events ADD COLUMN IF NOT EXISTS reused_pct SMALLINT")
            cur.execute("ALTER TABLE backup_events ADD COLUMN IF NOT EXISTS full_backup BOOLEAN")
            # Guest name from the backup notes (vzdump --notes-template '{{guestname}}').
            cur.execute("ALTER TABLE backup_events ADD COLUMN IF NOT EXISTS guest_name TEXT")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_backup_tasks (
                    upid TEXT PRIMARY KEY,
                    cluster_id BIGINT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
                    processed_at TIMESTAMPTZ NOT NULL
                );
                """
            )
            # These duplicated the UNIQUE constraints' own indexes.
            cur.execute("DROP INDEX IF EXISTS idx_vms_cluster_vmid")
            cur.execute("DROP INDEX IF EXISTS idx_nodes_cluster")
            cur.execute("DROP INDEX IF EXISTS idx_clusters_slug")
            # Foreign keys get no index of their own: the latest restore test is
            # looked up per VM on every overview, and a VM delete cascades here.
            cur.execute("CREATE INDEX IF NOT EXISTS idx_restore_tests_vm ON restore_tests(vm_id, tested_at DESC)")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_processed_backup_tasks_cluster ON processed_backup_tasks(cluster_id)"
            )
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
            # Restricts a PBS connection to one namespace subtree ('' = root only),
            # e.g. a pull-sync target holding this cluster's copies.
            cur.execute("ALTER TABLE pbs_connections ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL DEFAULT ''")
            cur.execute(
                "ALTER TABLE pbs_connections ADD COLUMN IF NOT EXISTS cert_fingerprints TEXT[] NOT NULL DEFAULT '{}'"
            )
            # Other names/IPs PVE or a sync remote uses for this PBS, for when
            # it cannot be recognised automatically (e.g. unreachable network).
            cur.execute("ALTER TABLE pbs_connections ADD COLUMN IF NOT EXISTS aliases TEXT[] NOT NULL DEFAULT '{}'")
            # Registered PBS behind a PVE storage, resolved at sync time
            # (fingerprint, name, DNS or the certificate the server serves).
            cur.execute("ALTER TABLE pve_pbs_targets ADD COLUMN IF NOT EXISTS pbs_id BIGINT")
            # After pbs_connections, which it references.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pbs_sync_jobs (
                    pbs_id BIGINT NOT NULL REFERENCES pbs_connections(id) ON DELETE CASCADE,
                    job_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    store TEXT NOT NULL,
                    ns TEXT NOT NULL DEFAULT '',
                    remote_name TEXT NOT NULL DEFAULT '',
                    remote_host TEXT NOT NULL DEFAULT '',
                    remote_fingerprint TEXT NOT NULL DEFAULT '',
                    remote_store TEXT NOT NULL DEFAULT '',
                    remote_ns TEXT NOT NULL DEFAULT '',
                    schedule TEXT NOT NULL DEFAULT '',
                    last_run_state TEXT NOT NULL DEFAULT '',
                    last_run_at TIMESTAMPTZ,
                    next_run_at TIMESTAMPTZ,
                    PRIMARY KEY (pbs_id, job_id)
                );
                """
            )
            cur.execute("ALTER TABLE pbs_sync_jobs ADD COLUMN IF NOT EXISTS remote_pbs_id BIGINT")
            cur.execute("ALTER TABLE pbs_sync_jobs ADD COLUMN IF NOT EXISTS running_since TIMESTAMPTZ")
            cur.execute("ALTER TABLE pbs_sync_jobs ADD COLUMN IF NOT EXISTS running_upid TEXT")
            # Progress is asked live and never stored (column of v2.26–v2.29).
            cur.execute("ALTER TABLE pbs_sync_jobs DROP COLUMN IF EXISTS running_percent")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS cluster_topology (
                    cluster_id BIGINT PRIMARY KEY REFERENCES clusters(id) ON DELETE CASCADE,
                    data JSONB NOT NULL,
                    built_at TIMESTAMPTZ NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pbs_datastore_usage (
                    pbs_id BIGINT NOT NULL REFERENCES pbs_connections(id) ON DELETE CASCADE,
                    store TEXT NOT NULL,
                    total_bytes BIGINT NOT NULL,
                    used_bytes BIGINT NOT NULL,
                    avail_bytes BIGINT NOT NULL,
                    estimated_full_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (pbs_id, store)
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

    # Apply any non-additive migrations registered in app.db_migrations
    from app.db_migrations import run_migrations

    run_migrations()


def bootstrap_script() -> str:
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "proxmox-bootstrap.sh"
    return script_path.read_text(encoding="utf-8")


def pbs_bootstrap_script() -> str:
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "pbs-bootstrap.sh"
    return script_path.read_text(encoding="utf-8")
