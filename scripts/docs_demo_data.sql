-- Applied after scripts/dev_seed.sql (see docs/dev-local.md, "Documentation screenshots").
-- Neutral demo names for documentation screenshots (dev DB only).
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT c.table_name, c.column_name FROM information_schema.columns c
           JOIN information_schema.tables t USING (table_name, table_schema)
           WHERE c.table_schema='public' AND t.table_type='BASE TABLE' AND c.data_type IN ('text','character varying')
  LOOP
    EXECUTE format('UPDATE %I SET %I = replace(replace(replace(replace(%I, ''aboe-muc'', ''berlin''), ''fh-muc'', ''hamburg''), ''aboe'', ''berlin''), ''HAM-'', '''') WHERE %I ~ ''aboe|fh-muc|HAM-''',
                   r.table_name, r.column_name, r.column_name, r.column_name);
  END LOOP;
END $$;

-- Three more clusters cloned from the first, with some variety.
DO $$
DECLARE src int; cid int; city text; copies int; v record; nv int; k int;
BEGIN
  SELECT id INTO src FROM clusters ORDER BY id LIMIT 1;
  FOR city, copies IN SELECT * FROM (VALUES ('frankfurt', 3), ('munich', 2), ('cologne', 1)) x(c, n) LOOP
    INSERT INTO clusters (name, slug, api_url, enrollment_secret, api_user, token_id, token_secret, fingerprint, registered_at, created_at,
                          last_pve_sync_at, last_pve_sync_ok, last_pve_sync_log, last_pve_sync_success_at, qdevice)
    SELECT 'pve-' || city, 'pve-' || city, replace(api_url, 'berlin', city), enrollment_secret || city, api_user, token_id, token_secret, fingerprint,
           registered_at, created_at, last_pve_sync_at, last_pve_sync_ok, last_pve_sync_log, last_pve_sync_success_at, NULL
    FROM clusters WHERE id = src RETURNING id INTO cid;
    INSERT INTO nodes (cluster_id, name, status) SELECT cid, replace(name, 'berlin', city), status FROM nodes WHERE cluster_id = src;
    FOR k IN 1..copies LOOP
      FOR v IN SELECT * FROM vms WHERE cluster_id = src LOOP
        INSERT INTO vms (cluster_id, node_name, vmid, name, backup_kind, last_backup_at, guest_type, ha, backup_storages, is_template)
        VALUES (cid, replace(v.node_name, 'berlin', city), v.vmid + k * 1000, replace(v.name, 'berlin', city) || CASE WHEN k > 1 THEN '-' || k ELSE '' END,
                v.backup_kind, v.last_backup_at, v.guest_type, v.ha, v.backup_storages, v.is_template)
        RETURNING id INTO nv;
        INSERT INTO backup_events (vm_id, started_at, finished_at, duration_seconds, size_bytes, encrypted, verify_state, status, upid, source,
                                   storages, transferred_bytes, throughput_bps, reused_pct, full_backup, guest_name, locations)
        SELECT nv, started_at, finished_at, duration_seconds, size_bytes, encrypted, verify_state, status, upid || city || k, source,
               storages, transferred_bytes, throughput_bps, reused_pct, full_backup, guest_name, locations
        FROM backup_events WHERE vm_id = v.id;
      END LOOP;
    END LOOP;
  END LOOP;
  -- Munich: the latest backups of two VMs failed (warnings on the overview).
  UPDATE backup_events SET status = 'failed'
  WHERE id IN (SELECT DISTINCT ON (e.vm_id) e.id FROM backup_events e JOIN vms m ON m.id = e.vm_id
               JOIN clusters c ON c.id = m.cluster_id WHERE c.slug = 'pve-munich' AND m.vmid IN (2112, 2113)
               ORDER BY e.vm_id, e.started_at DESC);
END $$;
UPDATE clusters SET last_pve_sync_ok = false, last_pve_sync_failure_started_at = now() - interval '50 minutes' WHERE slug = 'pve-munich';
SELECT name, slug FROM clusters ORDER BY id;
