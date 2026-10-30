# Automated Restore Testing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated Proxmox "PVE-Restore" scratch host that automatically restores each VM's latest complete backup, boots it in an isolated per-environment sandbox, verifies reachability/services, tears down (or holds for inspection), and records log + result into the existing restore-test data store on a per-VM schedule.

**Architecture:** New `app/services/restore_*` modules add the first *active* Proxmox client (API token + SSH) alongside the untouched read-only monitoring path. A single-slot worker, wired into the existing `auto_sync_loop`, drains a DB-persisted queue (`restore_runs`); held failures coexist, bounded by host RAM/disk and a static management-IP range. Production sync gains best-effort guest-IP collection. UI is server-rendered (no JS framework) in de/en.

**Tech Stack:** Python 3.12, FastAPI, PostgreSQL (psycopg), Fernet (`app/services/crypto.py`), Paramiko (new dep) for SSH, existing `httpx`-style PVE client patterns in `app/services/proxmox.py`, `app/services/proxmox_progress.py` for task progress.

**Spec:** `docs/superpowers/specs/2026-10-30-automated-restore-testing-design.md`

## Global Constraints

- Python target `py312`; ruff line-length 120; `double` quote style; isort via ruff. Copy verbatim from `pyproject.toml`.
- All new secrets (SSH private key, API token secret) stored via `app/services/crypto.encrypt` / `decrypt`; never logged, never returned to templates.
- No JavaScript framework (ADR 0003); server-rendered templates + minimal inline JS matching existing patterns. Smile Eyes styling; all user-facing strings in both `app/i18n/de.json` and `app/i18n/en.json`.
- DB: additive tables/columns via `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` in `app/db_core.py`; non-idempotent/data changes via a new numbered entry in `app/db_migrations.py` (never reuse/edit a version).
- The feature must never write to or destroy anything on a production cluster; all active/destructive actions target only the configured scratch host, gated by the host-identity + target-storage interlocks (spec §6, §12).
- DB-touching tests run under `BSENTINEL_DB_TESTS=1` (they TRUNCATE); pure-logic tests run always. Mirror the `tests/test_db.py` unittest style.
- Release discipline: fail-fast, fresh Postgres, CI checked after push to BOTH remotes (origin=GitLab, github); docs kept current.

## Review Focus

- **Wrong-host / wrong-storage reap** — if the configured SSH host or target storage is misconfigured to a production endpoint, the reaper must refuse and abort, never destroy (Task 8 tests the interlock with a non-matching host identity).
- **36 TB restore misjudged as stalled** — coarse per-1% progress >1 h between updates must NOT trigger abort while the PVE task is `running` (Task 10 tests liveness-by-task-status + adaptive backstop with a fake slow task).
- **Management-IP exhaustion** — when every IP in the configured range is held, a new run must *wait*, not crash or double-allocate (Task 5 tests allocation returns None → worker defers).
- **Failed run silently resets the due clock** — `failed`/`error`/`held` must write NO `restore_tests` row, so the VM stays overdue (Task 6 + Task 9 tests assert no row for non-passing results).
- **MAC regeneration on restore** — if a restore regenerates NIC MACs, DHCP reservations miss and the VM never gets its angestammte IP; the NIC-rewrite must preserve `macaddr` and only change bridge/tag (Task 8 tests the netN rewrite keeps the MAC).

---

## File Structure

New:
- `app/services/restore_config.py` — restore-server config model + load/save (encrypted), live storage enumeration.
- `app/services/restore_match.py` — 3-tuple `(pbs, datastore, namespace)` matching, auto-restorable set, latest-complete-snapshot selection.
- `app/services/restore_alloc.py` — VMID / VLAN-tag / management-IP allocation from configured ranges, in-use registry derived from `restore_runs`.
- `app/services/restore_server.py` — the active client: PVE API (write) + SSH (Paramiko). Bridge-ensure, restore (+progress), NIC rewrite, start/stop/destroy, LXC+dnsmasq helper provisioning, reaper, host-identity + free RAM/disk reads.
- `app/services/restore_probe.py` — probe-agent script (embedded) + worker-side client (query over mgmt IP) + result schema.
- `app/services/restore_ip_collect.py` — best-effort guest MAC/IP collection used by the production sync.
- `app/services/restore_worker.py` — queue worker: due selection, resource gating, the §11 pipeline, classify, persist, hold/retry/promote/reap, advisory lock.
- `app/db_restore.py` — DB layer for `restore_runs`, `vm_network_interfaces`, restore-server settings, vm_policy restore fields, allocation queries.
- `app/web/routes_restore.py` — settings section, per-VM controls, runs view, run/reap/retest/promote/pause endpoints.
- `templates/restore.html` + partials — runs view + held panel + host panel.
- `tests/test_restore_*.py` — one per module.

Modify:
- `app/db_core.py` — new tables + `vm_policies` restore columns.
- `app/types.py` — `RestoreRunRow`, `VMNetworkInterfaceRow`, `RestoreServerConfig`.
- `app/models.py` — pydantic payloads for config + per-VM policy.
- `app/web/sync.py` — enqueue due runs + start the single-slot worker + call IP collection.
- `app/services/proxmox.py` — add the few guest-agent/config read helpers for IP collection (read-only).
- `app/services/notifications.py` — `notify_restore_failed` / `notify_restore_held`.
- `app/db_governance.py` / `app/services/backup_status.py` — cadence-based due status for auto-restore VMs; surface last automated result + auto-restorable flag.
- `templates/settings.html` + `static/style.css` — restore-server settings section AND the cluster-tab layout fix (remove the redundant outer `table-card` box around the per-cluster cards).
- `templates/cluster.html` — per-VM restore controls + "Jetzt testen".
- `app/i18n/de.json`, `app/i18n/en.json` — new keys.
- `README.md`, `docs/admin-docs-*.md`, `docs/user-docs-*.md` — document the feature + operator prerequisites.

---

## Task 1: DB schema, types, and `db_restore` foundation

**Files:** Modify `app/db_core.py`, `app/types.py`; Create `app/db_restore.py`, `tests/test_restore_db.py`.

**Interfaces — Produces:**
- Tables `restore_runs` and `vm_network_interfaces` (columns per spec §14, §9) + `vm_policies` columns `auto_restore_enabled BOOLEAN DEFAULT FALSE`, `restore_cadence TEXT DEFAULT 'half_year'`, `probe_primary_ip TEXT`, `probe_tcp_ports INTEGER[] DEFAULT '{}'`, `probe_require_icmp BOOLEAN DEFAULT TRUE`, `hold_on_failure BOOLEAN DEFAULT FALSE`, `restore_retry_days INTEGER DEFAULT 7`, `next_run_at TIMESTAMPTZ`, `probe_window_seconds INTEGER`, `probe_first_delay_seconds INTEGER`, `probe_interval_seconds INTEGER`, `stall_backstop_factor REAL`.
- `RestoreRunRow`, `VMNetworkInterfaceRow` TypedDicts.
- `db_restore.create_run(vm_id, trigger) -> int`, `get_run(id) -> RestoreRunRow|None`, `update_run(id, **fields) -> None`, `list_runs(limit, status=None) -> list[RestoreRunRow]`, `active_and_held_runs() -> list[RestoreRunRow]`, `set_network_interfaces(vm_id, rows) -> None`, `get_network_interfaces(vm_id) -> list[VMNetworkInterfaceRow]`.

- [ ] **Step 1 — failing test** `tests/test_restore_db.py` (DB-gated, unittest style like `test_db.py`): `test_create_and_update_run` creates a run (`status='queued'`), updates `percent=42`, reads it back `== 42`; `test_network_interfaces_roundtrip` stores two NICs and reads them ordered by nic_index; `test_vm_policy_restore_defaults` inserts a bare policy and asserts `restore_cadence == 'half_year'`, `auto_restore_enabled is False`.
- [ ] **Step 2** Run `BSENTINEL_DB_TESTS=1 ... pytest tests/test_restore_db.py -v` → FAIL (tables/functions missing).
- [ ] **Step 3** Add the DDL in `app/db_core.py` (alongside the other `CREATE TABLE IF NOT EXISTS`), the TypedDicts in `app/types.py`, and implement `app/db_restore.py` using the `connect()` pattern.
- [ ] **Step 4** Run the tests → PASS.
- [ ] **Step 5** Commit `feat(restore): schema + db layer for restore runs and vm network interfaces`.

## Task 2: Restore-server config (encrypted) + live storage enumeration

**Files:** Create `app/services/restore_config.py`, `tests/test_restore_config.py`; Modify `app/models.py`, `app/types.py`, `app/db_restore.py`.

**Interfaces:**
- Consumes: `crypto.encrypt/decrypt`, `app_settings` via `db_settings`.
- Produces: `RestoreServerConfig` TypedDict (api fields, ssh fields, `target_storages: list[{storage,type,disk_class,order}]`, `sandbox_bridge`, `sandbox_vlan_pool`, `mgmt_iface`, `mgmt_vlan`, `mgmt_gateway`, `mgmt_netmask`, `mgmt_ip_from`, `mgmt_ip_to`, `probe_offset`, probe budget defaults, `stall_backstop_factor`, `hard_cap_seconds|None`); `restore_config.load() -> RestoreServerConfig|None`, `restore_config.save(cfg) -> None` (secrets encrypted), `restore_config.is_configured() -> bool`.

- [ ] **Step 1 — failing test** `test_config_roundtrip_encrypts_secrets`: save a config with `token_secret='s'` + `ssh_private_key='k'`; assert the raw `app_settings` value for those keys != plaintext, and `load()` returns them decrypted.
- [ ] **Step 2** Run → FAIL.
- [ ] **Step 3** Implement storage under the `restore_server.` settings prefix; one JSON blob for non-secret structure + separate encrypted keys for secrets. Pydantic model in `app/models.py` validates ranges (from ≤ to, netmask valid).
- [ ] **Step 4** PASS. **Step 5** Commit `feat(restore): encrypted restore-server config`.

## Task 3: Source matching + snapshot selection (`restore_match`)

**Files:** Create `app/services/restore_match.py`, `tests/test_restore_match.py`.

**Interfaces:**
- Consumes: `db_clusters.list_pbs_connections`, `pbs_sync_jobs`, the scratch host's local PBS storages (passed in as data).
- Produces: `match_source(vm, scratch_pbs_storages) -> SourceMatch|None` (exact 3-tuple `(pbs, datastore, namespace)`); `is_auto_restorable(vm, scratch_pbs_storages) -> bool`; `pick_latest_complete(snapshots) -> Snapshot|None` (completeness, not verification).

- [ ] **Step 1 — failing tests** `test_exact_tuple_match` (a VM whose `(pbs,datastore,namespace)` equals a scratch store → match; differing namespace → None); `test_not_restorable_when_absent`; `test_pick_latest_complete_ignores_incomplete` (picks newest `complete` snapshot, skips incomplete/partial).
- [ ] **Step 2** FAIL → **Step 3** implement pure functions → **Step 4** PASS → **Step 5** Commit `feat(restore): exact 3-tuple source matching`.

## Task 4: Allocation (`restore_alloc`)

**Files:** Create `app/services/restore_alloc.py`, `tests/test_restore_alloc.py`; add allocation query to `app/db_restore.py`.

**Interfaces:**
- Consumes: `db_restore.active_and_held_runs()` (for in-use VMIDs/tags/IPs), config ranges.
- Produces: `allocate(config, in_use) -> Allocation|None` returning `{vmid, vlan_tag, mgmt_ip}` (lowest free of each), or `None` if **no management IP is free** (caller must wait); `ip_range(from,to) -> list[str]`.

- [ ] **Step 1 — failing tests** `test_allocates_lowest_free`; `test_returns_none_when_mgmt_ips_exhausted` (all IPs in use → None); `test_skips_held_vmids_and_tags`.
- [ ] **Step 2** FAIL → **Step 3** implement (IP math via `ipaddress`) → **Step 4** PASS → **Step 5** Commit `feat(restore): environment resource allocation`.

## Task 5: Guest IP collection in production sync (`restore_ip_collect`)

**Files:** Create `app/services/restore_ip_collect.py`, `tests/test_restore_ip_collect.py`; Modify `app/services/proxmox.py` (read-only agent/config helpers), `app/web/sync.py` (call it), `app/db_restore.py` (store).

**Interfaces:**
- Consumes: read-only PVE API (`.../agent/network-get-interfaces`, `/lxc/{vmid}/config`, cloud-init `ipconfigN`).
- Produces: `collect_for_vm(vm, pve_get) -> list[NIC]` (`mac`, `ips`, `prefix`, source), `primary_ip(nics, override) -> str|None` (first non-loopback IPv4 unless overridden); persisted via `db_restore.set_network_interfaces`.

- [ ] **Step 1 — failing tests** `test_primary_is_first_non_loopback_v4`; `test_override_wins`; `test_missing_agent_yields_no_ips` (agent error → `[]`, no raise). Feed a fake `pve_get` returning canned agent JSON.
- [ ] **Step 2** FAIL → **Step 3** implement best-effort with per-VM try/except + bounded timeout; wire a call in the cluster sync path (guarded, must not slow/fail sync) → **Step 4** PASS → **Step 5** Commit `feat(restore): best-effort guest IP collection in sync`.

## Task 6: Cadence/due logic + enqueue

**Files:** Modify `app/services/backup_status.py`, `app/db_governance.py`, `app/db_restore.py`; Create `tests/test_restore_schedule.py`.

**Interfaces:**
- Produces: `cadence_days(cadence) -> int` (month=30, quarter=91, half_year=182, year=365); `due_auto_restore_vms() -> list[int]` (enabled + auto-restorable + due by cadence/`next_run_at`, not already queued/running/held); `record_result(run, result)` writing a `restore_tests` row ONLY for `passed`/`limited`.

- [ ] **Step 1 — failing tests** `test_cadence_days_mapping`; `test_due_includes_never_tested_enabled`; `test_failed_result_writes_no_restore_test_row` (clock not reset); `test_passed_result_writes_row`.
- [ ] **Step 2** FAIL → **Step 3** implement; extend governance due-status to use cadence for enabled VMs (spec §8) → **Step 4** PASS → **Step 5** Commit `feat(restore): cadence scheduling and result-to-clock semantics`.

## Task 7: Probe agent + worker-side probe client (`restore_probe`)

**Files:** Create `app/services/restore_probe.py`, `tests/test_restore_probe.py`.

**Interfaces:**
- Produces: `PROBE_AGENT_SCRIPT` (embedded script that, given targets, runs ICMP + TCP socket checks and serves/returns structured JSON `{ip:{icmp:bool,ports:{p:bool},latency_ms,ts}}`); `probe_once(mgmt_ip, token, targets) -> ProbeResult` (worker-side client); `classify(result, require_icmp, tcp_ports, had_ip) -> Literal['passed','limited','failed']` per spec §13.

- [ ] **Step 1 — failing tests** of `classify`: `test_passed_requires_icmp_and_all_ports`; `test_limited_when_no_ip`; `test_limited_when_port_closed`; `test_failed_when_unreachable`. (Pure function; the network client is tested via a fake transport.)
- [ ] **Step 2** FAIL → **Step 3** implement `classify` + the agent script text + a thin client with injectable transport → **Step 4** PASS → **Step 5** Commit `feat(restore): probe agent and result classification`.

## Task 8: Active restore-server client (`restore_server`)

**Files:** Create `app/services/restore_server.py`, `tests/test_restore_server.py`; add `paramiko` to deps (`requirements.txt`/`pyproject`).

**Interfaces:**
- Produces a class `RestoreServer(config)` with: `verify_host_identity()` (interlock), `free_resources() -> {ram_free, per_store_disk_free}`, `list_pbs_storages()`, `list_target_storages()`, `ensure_sandbox_bridge()` (idempotent, portless, VLAN-aware, via SSH), `restore(vmid, source, target_storages, on_progress)` (streams task log + polls progress; liveness by task status; adaptive stall backstop), `rewrite_nics(vmid, vlan_tag)` (bridge+tag only, **MAC preserved**), `start(vmid)`, `shutdown(vmid)`, `destroy(vmid)`, `provision_helper(vmid, vlan_tag, mgmt_ip, reservations, token)`, `reap(spare_owned)` (destroy everything not owned by active/held).

- [ ] **Step 1 — failing tests** (inject a fake SSH + fake PVE client; no real host): `test_verify_host_identity_refuses_production` (host matches a known cluster endpoint → raises); `test_rewrite_nics_preserves_macaddr` (parses a `net0=virtio=AA:BB:..,bridge=vmbrX` and rewrites only bridge+tag); `test_reaper_spares_owned_destroys_rest`; `test_restore_aborts_on_task_error_not_on_quiet` (fake task `running` with no progress for a long simulated time → no abort; task `error` → abort).
- [ ] **Step 2** FAIL → **Step 3** implement over an injectable SSH/HTTP boundary (the real Paramiko/PVE calls behind thin seams so tests use fakes). **Step 4** PASS → **Step 5** Commit `feat(restore): active PVE+SSH restore-server client`.

## Task 9: Worker pipeline (`restore_worker`)

**Files:** Create `app/services/restore_worker.py`, `tests/test_restore_worker.py`.

**Interfaces:**
- Consumes: Tasks 2–8 interfaces.
- Produces: `run_one(run_id, server, config)` executing spec §11 steps against an injected `RestoreServer`; `pick_next() -> run_id|None` honouring RAM/disk **skip** and mgmt-IP **wait**; `promote_held(run_id)`, `reap_held(run_id)`, `retest_held(run_id)`; `reconcile_on_startup()`.

- [ ] **Step 1 — failing tests** (fake `RestoreServer` + probe): `test_passed_run_writes_restore_test_and_reaps`; `test_failed_with_hold_keeps_env_and_frees_slot` (status `held`, allocation retained, no `restore_tests` row); `test_retest_green_promotes_and_reaps`; `test_pick_next_skips_too_big_waits_on_no_ip`; `test_startup_marks_running_as_error_spares_held`.
- [ ] **Step 2** FAIL → **Step 3** implement the pipeline + classification + persistence + hold/retry/promote; advisory-lock the active slot → **Step 4** PASS → **Step 5** Commit `feat(restore): single-slot restore worker pipeline`.

## Task 10: Wiring into the app loop

**Files:** Modify `app/web/sync.py`, `app/main.py` (if needed), `app/services/notifications.py`; Create `tests/test_restore_loop.py` (light).

- [ ] **Step 1 — failing test** `test_enqueue_step_creates_queued_runs_for_due` (fake due list → rows created once, not duplicated).
- [ ] **Step 2** FAIL → **Step 3** add an enqueue+drain step to `auto_sync_loop` using a dedicated 1-slot executor; call `restore_ip_collect` in the sync; add `notify_restore_failed/held`; `reconcile_on_startup` in lifespan. → **Step 4** PASS → **Step 5** Commit `feat(restore): wire scheduler, worker, IP collection, notifications`.

## Task 11: Settings UI — restore-server section + cluster-layout fix

**Files:** Modify `templates/settings.html`, `static/style.css`, `app/web/routes_restore.py` (new), `app/web/routes_settings.py` (context), i18n.

- [ ] **Step 1** Add a "Restore-Server" tab/section: config form (credentials, SSH key, mgmt-net gateway/mask/range, sandbox bridge + VLAN pool, target-storage picker with type + disk-class label + order, probe defaults), a "Verbindung testen + Sandbox-Bridge sicherstellen" action. Bind to `restore_config`.
- [ ] **Step 2 — cluster layout fix:** remove the redundant outer `<section class="table-card" id="cluster">` wrapper so the per-cluster `fieldset` cards are not boxed-in-a-box (spec: "ein Kasten zu viel"); adjust `.settings-cluster-list`/`.settings-cluster-card` spacing in `static/style.css`.
- [ ] **Step 3 — verify** via the browser preview at 1920px: screenshot the cluster tab (no nested box, equal spacing) and the restore-server section; fix CSS until clean.
- [ ] **Step 4** Commit `feat(restore): settings UI + fix cluster settings double-box layout`.

## Task 12: Per-VM controls + "Jetzt testen"

**Files:** Modify `templates/cluster.html`, `app/db_governance.py` (expose fields), `app/web/routes_restore.py`, i18n.

- [ ] **Step 1** Add per-VM: auto-restore toggle (disabled + "nicht auto-restaurierbar (kein Offsite-Backup)" when not matchable), cadence select, primary-IP override, TCP ports, require-ICMP, retry days, "nächster Lauf am…", **"Jetzt testen"** button (enqueues a manual run). **Step 2** POST endpoints update `vm_policies` / enqueue. **Step 3** Verify render via preview. **Step 4** Commit `feat(restore): per-VM restore-test controls`.

## Task 13: Runs view + held panel + host panel

**Files:** Create `templates/restore.html` (+ partials); Modify `app/web/routes_restore.py`, nav in `templates/base.html`, i18n.

- [ ] **Step 1** Runs list (status, duration, **live percent/ETA/throughput** for running), full log + structured checks per run; **host panel** (free RAM, free disk per store, live); **held environments** panel (VMID/VLAN/mgmt-IP + "erneut testen" / "Abräumen", and the green-re-test confirm "als erfolgreichen Restore dokumentieren und abräumen?"); **queue pause/resume**. **Step 2** Endpoints for run/reap/retest/promote/pause. **Step 3** Verify via preview. **Step 4** Commit `feat(restore): runs view, held environments, host capacity panel`.

## Task 14: Docs, i18n completeness, external descriptions, Outline

**Files:** `README.md`, `docs/admin-docs-*.md`, `docs/user-docs-*.md`, both i18n files; GitLab + GitHub project descriptions; Outline wiki.

- [ ] **Step 1** Document the feature, the operator prerequisites (dedicated scratch host; local-only PBS; `VM.GuestAgent.Audit` on prod tokens; portless bridge; mgmt-net range). **Step 2** Verify de/en key parity (extend `tests/test_i18n.py` coverage if it checks parity). **Step 3** Update GitLab/GitHub descriptions + the Outline page (`~/.outline-token`). **Step 4** Commit `docs(restore): document automated restore testing`.

## Task 15: Verify + major release

- [ ] **Step 1** Full verification: `ruff check` + `ruff format --check`; `pytest` (pure) and `BSENTINEL_DB_TESTS=1 pytest` against a fresh throwaway Postgres; app boots (`/healthz`); migrations apply clean on an empty DB.
- [ ] **Step 2** Bump `APP_VERSION` in `app/config.py` to the next **major** (`v3.0.0`); add a `## [v3.0.0]` CHANGELOG section.
- [ ] **Step 3** Commit `release: v3.0.0 — automated restore testing`; tag `v3.0.0`.
- [ ] **Step 4** Push branch → merge to `main` → push `main` + tag to **both** remotes (origin=GitLab triggers deploy, and github). Check CI on both after push.
