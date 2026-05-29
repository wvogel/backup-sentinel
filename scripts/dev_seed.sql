-- Sample data for local browser testing. Idempotent: wipes and re-seeds.
-- Covers: long node names (wrap testing), a yearly restore policy in WARNING,
-- a quarterly restore policy in WARNING, and several "never tested" criticals,
-- plus 14 days of backup events so the sparkline renders.
TRUNCATE clusters RESTART IDENTITY CASCADE;

INSERT INTO clusters (name, slug, api_url, enrollment_secret, registered_at, created_at,
                      last_pve_sync_at, last_pve_sync_ok, last_pve_sync_success_at)
VALUES ('pve-aboe-muc', 'pve-aboe-muc', 'https://pve.example:8006', 'secret', now(), now(),
        now(), true, now());

INSERT INTO nodes (cluster_id, name, status) VALUES
 (1, 'pve1-aboe-muc', 'online'),
 (1, 'pve2-aboe-muc', 'online'),
 (1, 'pve3-aboe-muc', 'online');

INSERT INTO vms (cluster_id, node_name, vmid, name, backup_kind, last_backup_at) VALUES
 (1, 'pve1-aboe-muc', 112,  'fw1-aboe-muc', 'daily', now() - interval '1 day'),
 (1, 'pve3-aboe-muc', 113,  'fw2-aboe-muc', 'daily', now() - interval '6 hours'),
 (1, 'pve2-aboe-muc', 1104, 'HAM-APP02',    'daily', now() - interval '5 hours'),
 (1, 'pve3-aboe-muc', 1401, 'HAM-APP03',    'daily', now() - interval '5 hours'),
 (1, 'pve2-aboe-muc', 1105, 'HAM-REF',      'daily', now() - interval '4 hours'),
 (1, 'pve3-aboe-muc', 1102, 'HAM-TMS01',    'daily', now() - interval '5 hours');

-- vmid 113 -> yearly (365d), vmid 1104 -> quarterly (90d), rest -> default
INSERT INTO vm_policies (vm_id, owner_name, criticality, restore_interval_days, restore_required, rpo_hours, notes)
SELECT id, 'IT', 'high',
       CASE vmid WHEN 113 THEN 365 WHEN 1104 THEN 90 ELSE NULL END,
       true, 24, ''
FROM vms;

-- yearly tested 340d ago -> within 30d warn lead -> WARNING
INSERT INTO restore_tests (vm_id, tested_at, result, recovery_type, duration_minutes, created_at)
SELECT id, current_date - 340, 'passed', 'full', 2, now() FROM vms WHERE vmid = 113;
-- quarterly tested 80d ago -> within 14d warn lead -> WARNING
INSERT INTO restore_tests (vm_id, tested_at, result, recovery_type, duration_minutes, created_at)
SELECT id, current_date - 80, 'passed', 'full', 5, now() FROM vms WHERE vmid = 1104;

-- 14 days of OK backup events per VM for the sparkline
INSERT INTO backup_events (vm_id, started_at, finished_at, duration_seconds, size_bytes, encrypted, status, source)
SELECT v.id, now() - (g || ' days')::interval, now() - (g || ' days')::interval + interval '20 min',
       1200, 5000000000, true, 'ok', 'pbs'
FROM vms v, generate_series(0, 13) g;
