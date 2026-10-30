# Automated Restore Testing — Design

Status: draft for approval · Date: 2026-10-30

## 1. Intent and why

Backup Sentinel today *monitors* backups and tracks **manually recorded**
restore tests (`restore_tests`: vm_id, tested_at, result, recovery_type,
duration_minutes, evidence_note). Governance nags when a VM's restore test is
overdue, but a human still has to actually perform the restore and type in the
result.

This feature adds a **dedicated Proxmox restore server** ("PVE-Restore") and
makes the restore test **automatic and verifiable**: on a schedule (or on
demand), the system restores a VM's latest complete backup onto the scratch
host, boots it in a fully isolated sandbox network, confirms it comes up on its
real IP(s) and optionally that configured TCP services answer, then tears
everything down and records the full log + result into the same data store the
manual flow uses.

It is the **first active/destructive** part of the product. Everything against
production clusters stays read-only; all write/destroy actions happen **only**
on the dedicated scratch host.

## 2. Goals

- Restore a VM from backup onto the PVE-Restore host, boot it, verify
  reachability, tear it down, all unattended.
- Per-VM schedule: **month / quarter / half-year (default) / year**; a global
  FIFO queue runs due tests; **never two active tests at once** (one restore+
  boot+probe pipeline at a time). Parked **held** environments (kept for manual
  inspection after a failure) may coexist with the queue and with each other,
  bounded only by host RAM/disk, which the operator manages manually.
- Verify on the VM's **real/angestammte IP(s)** from an isolated sandbox, never
  touching production or any physical switch.
- Default check is **ICMP echo**; optional per-VM **TCP port** checks
  (e.g. 3389, 445, 443, 80).
- Persist the **full restore log** and the **per-check results** into the
  existing restore-test data store so dashboards, coverage and reports keep
  working unchanged.
- Hard, self-healing cleanup: after a test the scratch host returns to empty —
  except environments intentionally **held** for manual inspection, which persist
  until the operator reaps them.
- Manual "run now" from the UI.

## 3. Non-goals

- No restore onto, or any write against, a production cluster.
- No restore from a **remote** PBS. The scratch host only ever has **local**
  PBS datastores configured (multiple, one per source PBS+datastore+namespace);
  restoring from local is therefore the only path by construction.
- **No management of the replication.** Getting production backups into those
  local datastores (PBS pull/sync jobs, per datastore+namespace) is provisioned
  **externally on the restore host**. The feature neither creates, triggers, nor
  waits for a sync — it restores whatever complete snapshot is already local. A
  stale local copy is the operator's concern, not this feature's.
- No "verified" requirement on the backup — we restore the latest **complete**
  snapshot (completeness, not PBS verification state).
- No application-level/data-integrity testing beyond ICMP + configured TCP
  ports in this first step.
- No support for more than one restore server in the first version (single
  scratch host; the schema should not make a second one impossible later).

## 4. Anti-goals (what makes this a failure even if it "works")

- Any traffic from the sandbox reaching a physical switch or production.
- The reaper destroying something that is **not** on the dedicated scratch host
  (a wrong-host misconfiguration must fail safe, never delete production).
- Leaving **untracked** guests/volumes/leftovers on the scratch host after a run
  (intentionally held environments excepted — those are tracked and reaped on
  demand).
- Blocking or slowing the existing production sync, or coupling restore
  failures to monitoring.
- Two restore tests overlapping.
- Secrets (SSH key, API token) landing in logs or the DB in plaintext.

## 5. Constraints (the existing system this plugs into)

- **Stack:** FastAPI single process; a background `auto_sync_loop()` (asyncio)
  offloading to a `ThreadPoolExecutor`; PostgreSQL via `app/db_core.connect()`;
  server-rendered templates, **no JS framework** (ADR 0003); server-side i18n
  (de/en, both must be filled).
- **Secrets:** symmetric Fernet via `app/services/crypto.py`
  (`encrypt`/`decrypt`, `BSENTINEL_SECRET_KEY`). All new secrets use it.
- **Proxmox client is read-only today** (`app/services/proxmox.py`: only
  `_pve_get*`). This feature introduces the first write/SSH client, kept in new
  modules; the production path stays read-only.
- **Credentials pattern:** clusters/PBS are rows with API token id/secret +
  fingerprint; `app_settings` is a key/value store; `append_audit_event`
  already reserves a `restore_test.` audit namespace.
- **Governance model:** `vm_policies` (criticality, restore_interval_days,
  restore_required, rpo_hours…); class intervals high=90/medium=180/low=365;
  due-status logic in `app/db_governance.py` / `app/services/backup_status.py`.
- Follow the project's release discipline (fail-fast release script, fresh
  Postgres, CI green, docs + Outline updated) — out of scope for the design but
  binds the implementation.

## 6. The scratch host (PVE-Restore) configuration

Stored once (single restore server), secrets Fernet-encrypted. Configured in a
new Settings section. The target storage and sandbox become selectable **after**
credentials are saved (we enumerate them live via the API).

Fields:

- **Identity/API:** name, `api_url`, `api_user`, `token_id`, `token_secret`,
  `fingerprint` (same shape as a cluster).
- **SSH:** host, port (22), user (root), **private key** (encrypted). Used for
  the actions with no clean API (LXC/dnsmasq orchestration, network-interface
  ensure, hard sweep). Known-host/fingerprint pinned.
- **Target storage(s) for restored disks:** one or more of the host's regular
  storages (via `fetch_storage_config`), each shown with its **PVE type**
  (zfs / nfs / lvmthin / dir / …) so the operator sees what it is. Because
  Proxmox cannot know the underlying media, each target storage also carries a
  **manual disk-class label**: `enterprise-ssd | standard-ssd | sas-hdd |
  sata-hdd`. The operator sets an **ordered preference** (preferred first);
  normally all of a VM's disks go to the preferred storage, but a VM too large
  for one storage **spills** to the next in the list. These target storages are
  the scratch area that gets purged after each run. (Per-VM "prefer disk-class
  X" is a possible later refinement; not in v1.)
- **Sandbox bridge:** a fixed configured Linux bridge **interface name**
  (valid ifname, ≤15 chars, default `vmbr9`; UI label "Restore Sandbox"). The
  app **ensures it exists, is VLAN-aware, and has no physical port**
  (`bridge-ports none`) via SSH, idempotently. Isolation is guaranteed by the
  portless bridge; per-environment VLAN tags isolate coexisting environments
  from each other (§10).
- **Sandbox VLAN pool:** the range of VLAN ids the app may allocate per
  environment (default e.g. `900–999`). One tag per live environment.
- **Probe container image/template:** a small Alpine/Debian LXC template on the
  host, or a tiny image we ensure; runs `dnsmasq` + the probe agent. Name
  configurable (default template id/volid).
- **Helper management network** (how the worker reaches the helpers) —
  configured explicitly, **no DHCP**: management **interface/bridge**
  (e.g. `vmbr0`), **VLAN-id**, **gateway**, **netmask**, and an **IPv4 range
  (from–to)**. Each helper gets a **static** management IP that the app
  **allocates from this range and tracks as in-use** (freed on teardown/reap);
  the app remembers which are in use. The Backup Sentinel app host must be able
  to reach this range. Separate from the isolated sandbox bridge. If the range
  is exhausted, the queue **waits** (§15). (This is the helper's *management*
  leg only; the sandbox leg still serves the VM via dnsmasq per §10.)
- **Probe host address offset:** which host number the helper takes in each
  subnet (default `.254`), with collision-avoidance against the VM's own IPs.
- **Time budgets (overridable per VM).** The **restore phase has no fixed total
  timeout** — a 40 GB guest returns in minutes, a 35 TB one takes hours, both
  fine as long as they progress. **Liveness = the PVE restore task's own status**
  (`running` vs stopped/errored): while the task is `running` the restore is
  alive, and a long quiet period is normal (on a 36 TB guest a single 1% step can
  take well over an hour). The worker **aborts only** on a **task error**, or —
  as a backstop against a genuine hang — if the task is `running` yet shows **no
  progress for far longer than the expected per-percent time**, which is derived
  **adaptively** from size × observed/seeded throughput with a high floor. There
  is **no small fixed stall grace** and no fixed total cap. The **probe** phase
  keeps bounded budgets: `probe_window_seconds` (reachability window after boot;
  default 300), `probe_first_delay_seconds` (30), `probe_interval_seconds` (30).

A **safety interlock:** the restore worker refuses to run unless every resolved
target storage is one of the configured scratch target storages, and refuses if
the configured SSH host resolves to any known production cluster/PBS endpoint.

## 7. Mapping a backup to its source cluster/PBS

We already know, from the existing configuration, where each VM is backed up and
which copies were pull-synced from there — `pbs_connections` (PBS name,
datastore, namespace per cluster) and `pbs_sync_jobs` (pull relationships). The
scratch host's own local PBS exposes the datastores+namespaces it holds.

**Matching is always exact, on the 3-tuple `(PBS name, datastore, namespace)`** —
never fuzzy, never name-approximate. A VM is **auto-restorable** iff one of its
backup locations' 3-tuple is present on the scratch host. Within the matched
datastore+namespace, the VM's snapshots are found by VMID (name is a logged
sanity check), and we pick the **latest complete** one.

A VM whose 3-tuple the scratch host does **not** see is simply **not
auto-restorable** — these are the systems without an offsite/synced copy. This
is a **visible state**, not an error and not a silent skip: such VMs are shown
as "not auto-restorable (no offsite backup)" in the host/governance list and are
never enqueued. `error` is reserved for a VM that *should* be restorable but hits
an infra/mapping fault at run time (e.g. snapshot vanished, SSH down).

## 8. Per-VM policy additions

Extend `vm_policies` (all `ADD COLUMN IF NOT EXISTS`, with defaults, so existing
rows stay valid):

- `auto_restore_enabled BOOLEAN DEFAULT FALSE` — opt-in per VM.
- `restore_cadence TEXT DEFAULT 'half_year'` — one of
  `month|quarter|half_year|year`. Drives the automated schedule and the
  governance due-date for enabled VMs (mapped to days: 30/91/182/365).
- `probe_primary_ip TEXT` — manual override of which collected IP is "primary".
- `probe_tcp_ports INTEGER[]` — additional TCP ports to check (default empty =
  ICMP only).
- `probe_require_icmp BOOLEAN DEFAULT TRUE` — allow ports-only VMs that block
  ICMP.
- `hold_on_failure BOOLEAN DEFAULT FALSE` — when a run does **not** pass, keep
  the restored environment up for manual inspection instead of tearing it down
  (see §11/§15 for the queue-pause consequence).
- `restore_retry_days INTEGER DEFAULT 7` — after a `failed`/`error` run, how
  long until the VM is re-enqueued. Manually adjustable per VM.
- `next_run_at TIMESTAMPTZ` — manual override of when the next run is due
  (set by the operator; otherwise derived from cadence / retry).
- Optional per-VM overrides of the probe budgets and of the adaptive stall
  backstop (its multiplier/floor — not a fixed seconds value; nullable, fall
  back to global). No fixed restore total timeout.

Cadence integrates with existing restore-due logic: for `auto_restore_enabled`
VMs the cadence-derived interval replaces the class interval; non-enabled VMs
keep today's behaviour.

## 9. Collecting the VM's real IPs (production sync extension)

Today no guest IPs are stored. Extend the **read-only** production sync to
collect, per running VM:

- **MAC per NIC** from the config (`netN=...,macaddr=...` for qemu;
  lxc `netN`).
- **MAC→IP mapping** from the QEMU guest agent
  (`/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces`), fallback for lxc
  from `/lxc/{vmid}/interfaces` or `ip=` in config, fallback cloud-init
  `ipconfigN`.

Store in a new `vm_network_interfaces` table (vm_id, nic_index, mac,
ip addresses[], prefix, is_primary, source, seen_at). Primary = first
non-loopback IPv4, overridable by `probe_primary_ip`.

This is additive and best-effort: agent missing/off → no IPs for that VM. Must
not fail or materially slow the sync (bounded timeouts, per-VM try/except).
A VM with no discoverable IP can still be restored/booted; it just can't be
network-verified → result `limited`.

**Operator prerequisite:** the guest-agent query needs `VM.GuestAgent.Audit`
(or `VM.Monitor`) on the production cluster API token. Current inventory tokens
may not have it; this is documented as a required privilege bump. Where the
privilege or the agent is absent, the per-VM `probe_primary_ip` override becomes
the primary IP source rather than a mere override — otherwise such VMs always
land as `limited`.

## 10. The sandbox network model

- One **VLAN-aware, portless Linux bridge** (the configured sandbox bridge) on
  the scratch host. No uplink ⇒ L2 never leaves the host, whatever the tag.
- **One VLAN tag per environment.** Because an active test and any number of
  held environments can run at once (§15), each restore environment gets a
  **unique VLAN tag** allocated for its lifetime (freed on reap). The restored
  VM and its helper share that tag; environments are isolated from each other at
  L2, so reused production subnets/IPs across environments never conflict. (The
  configured "sandbox VLAN" from §6, if set, is the base/pool.)
- **Helper container** = a tiny LXC, **dual-homed**:
  - **Sandbox leg(s)** on the portless bridge: runs **dnsmasq as a DHCP server**
    and the probe agent. It gets **one IP per distinct VM subnet** (aliases on
    its sandbox leg; a flat L2 shared segment), using the configured host-offset,
    avoiding the VM's own addresses.
  - **Management leg** on the configured management interface/VLAN with the
    configured static IPv4 — the only channel over which the worker talks to the
    helper.
  - The helper **must not route/forward between the two legs** (`ip_forward=0`,
    no NAT/bridging between sandbox and management). The restored VM stays on the
    portless bridge only and cannot reach the management network through the
    helper. The helper is a dual-homed **endpoint**, not a router.
- dnsmasq holds **static reservations MAC→angestammte-IP** for every NIC of the
  restored VM (from §9), plus a `dhcp-range` per subnet. No real gateway;
  options point harmlessly at the helper. Short lease.
- The restored VM's NICs are re-pointed to the sandbox bridge **keeping their
  original MACs** (only `bridge`/`tag` change in each `netN`, never `macaddr`).
- **Boot order:** sweep → restore disks → rewrite NICs to sandbox bridge →
  start helper (dnsmasq up and settled) → start VM. DHCP-configured guests pull
  their real IPs from the helper; static guests already have them. Either way
  the VM comes up on its angestammte IP(s).
- Probing runs **from inside the helper** (same L2) via a small **probe agent**:
  ICMP echo and configured **TCP socket** connects against the VM's IP(s), built
  to extend later to richer socket/service checks (handshake, banner, HTTP
  status, …).
- **Result handoff runs over the helper's management leg**, never over the
  sandbox network. The probe agent exposes a minimal authenticated endpoint
  (per-run token injected at container create) on the management IPv4; the worker
  polls it for **structured JSON** (per IP, per check: ok/fail, latency,
  timestamp, error). `pct exec` over SSH remains the bootstrap/fallback for
  configuring and starting the container. Because the helper never forwards
  between legs, exposing this endpoint does not weaken the VM's isolation.

## 11. One restore run (worker steps)

1. **Claim** the single global slot (Postgres advisory lock + single-worker
   executor). Mark run `running`.
2. **Reaper / clean slate** (see §12): destroy everything on the scratch host
   that isn't this run's just-created objects.
3. **Resolve** source datastore+namespace and latest **complete** snapshot
   (§7). Resolve target storage(s) (safety interlock, §6). **Allocate** this
   environment's resources: a **free VMID** (avoiding held environments and the
   helper), a **VLAN tag** from the sandbox pool, and a **static management IPv4**
   from the configured range (recorded as in-use). If no management IP is free,
   the run does not start and the queue waits (§15).
4. **Restore** the VM to the target storage under the allocated VMID (API
   restore; all disks → scratch storage, spilling across target storages if
   needed; config/MACs preserved). Stream the restore task log **and poll task
   progress** (bytes done/total → percent, throughput, ETA), updating the run
   row **live** (updates are coarse on huge guests — per 1%, possibly >1 h apart
   — ETA is still computed from elapsed/percent and throughput). Treat the PVE
   task status as liveness; abort only on a **task error** or a genuine **hang**
   (adaptive stall backstop scaled to the expected per-percent time, never a
   small fixed grace) — no fixed total clock.
5. **Rewrite NICs** to the sandbox bridge with this environment's **VLAN tag**,
   MACs preserved.
6. **Start helper** container: ensure template; attach its sandbox leg(s) to the
   portless bridge on this environment's VLAN tag and its management leg to the
   management interface/VLAN with the allocated management IPv4; configure
   dnsmasq with this VM's MAC→IP reservations and subnets; inject the per-run
   agent token; start the probe agent.
7. **Boot** the restored VM.
8. **Probe loop:** wait `probe_first_delay_seconds`, then every
   `probe_interval_seconds` until success or `probe_window_seconds`: the worker
   queries the helper's probe agent over the management IPv4; the agent runs
   ICMP (if required) + all configured TCP socket checks against the primary IP;
   all collected IPs are probed. The agent returns structured JSON per attempt,
   which the worker accumulates into the run's `checks`.
9. **Classify** the result (§13).
10. **Teardown:** shut down VM, stop+destroy helper, destroy VM, purge its disks,
    and **free** the VMID, VLAN tag and management IP. **Exception —
    hold-on-failure:** if the result is `failed`/`error` and the VM's
    `hold_on_failure` is set, **skip teardown**: leave the restored VM **and its
    helper (with the probe agent still running)** up as a tracked **held
    environment** (its VMID/tag/mgmt-IP stay allocated); the reaper will not
    touch it. The queue is **not** paused — the next run proceeds — unless the
    operator pauses it manually (§15). The operator can re-test the standing
    environment and reaps each held environment individually when done (§15).
11. **Reaper** again (belt and braces): remove this run's own leftovers and any
    untracked junk, **sparing** every tracked held environment.
12. **Persist** the run (§14) and **release the active slot** so the queue
    continues. On any exception the slot is released, this run's objects/junk
    swept (held environments spared), and the run recorded as `error`
    (hold-on-failure applies to a failed/error run too, if set).

The restore phase (steps 4–7) is bounded by **stall-detection, not a fixed
clock**; the probe window caps step 8.

## 12. The reaper (hard cleanup)

The scratch host is a scratch host: nothing legitimate ever lives there. The
reaper runs at **run start, run teardown, and app startup**, and:

- Enumerates all guests (qemu + lxc) and all volumes on the scratch storage.
- Destroys everything **not** owned by the currently-active run or by a tracked
  **held** environment (ownership = our recorded VMIDs + naming/tag
  `bsentinel-restore`, and the per-environment helpers). With no active run and
  no holds, it destroys **everything**.
- **Never touches a tracked held environment** — those persist until the
  operator reaps them individually (§15).
- Stops before touching anything if the host identity doesn't match the
  configured scratch host (fail-safe against misconfiguration).

On startup, any run left `running` (crash) is marked `error` and swept (held
environments spared). Runs left `held` survive restart: they stay `held` and
their environments are preserved until the operator reaps them.

## 13. Result classification

- **passed** — restored + booted + primary IP ICMP-reachable (unless
  `probe_require_icmp=false`) **and** every configured TCP port open within the
  window.
- **limited** — restored + booted, but not fully network-verified: no
  discoverable IP to probe, or ICMP/TCP partially failing within the window.
- **failed** — restore failed, VM never booted, or nothing reachable at all.
- **error** — misconfiguration/mapping/infra problem (no snapshot, host
  mismatch, SSH down). Distinct `restore_runs` status, treated like `failed` for
  scheduling/alerting; like `failed` it writes **no** `restore_tests` row, so it
  never resets the due clock.

## 14. Data model & persistence

Keep the existing `restore_tests` as the **canonical summary** (so coverage,
dashboards, PDF/weekly reports, governance keep working). **Only `passed` and
`limited` runs write a `restore_tests` row** (`recovery_type='auto-full-vm'`,
`result`, `duration_minutes`, `tested_at`, short `evidence_note`). Because the
due-date logic resets off the latest `restore_tests.tested_at`, this means
`passed`/`limited` reset the due clock while **`failed`/`error` deliberately do
not** — a broken restore capability stays overdue and visible instead of going
silent for months. `failed`/`error` are fully recorded in `restore_runs` (log +
checks) and surfaced as "last run failed" in the governance view. A `held` run
that the operator promotes on a green re-test (§15) becomes `passed` at that
point and writes its `restore_tests` row then.

Add an operational table `restore_runs` for the rich record:

- id, vm_id, trigger (`scheduled|manual`), status
  (`queued|running|passed|limited|failed|error|held|canceled`),
- allocated environment: `restore_vmid`, `vlan_tag`, `mgmt_ip` — these double as
  the **in-use registry**: an id/tag/IP is free iff no `running`/`held` row holds
  it; allocation picks the lowest free value from the configured ranges,
- source datastore/namespace, snapshot id, target storage,
- queued_at, started_at, finished_at, duration_seconds,
- **live progress:** `bytes_total`, `bytes_done`, `percent`, `throughput_bps`,
  `eta` — updated during the run; the final throughput is kept to seed future
  ETA estimates for that VM,
- `log TEXT` — the **complete** restore + orchestration log,
- `checks JSONB` — structured per-IP/per-port results and timings,
- link to the created `restore_tests.id`.

A `restore_queue` concept can be just `restore_runs` rows in `queued` status
(FIFO by queued_at); no separate table needed.

## 15. Scheduling, queue & concurrency

- The existing `auto_sync_loop` gains a step: enqueue due auto-restore VMs
  (enabled, auto-restorable per the §7 3-tuple match, cadence-based, not already
  queued/running) as `restore_runs(queued)`. "Due" means the enabled VM's
  cadence-derived due-status is `open` (never tested), `warning` or `critical`. On first rollout this can enqueue many VMs at once;
  that is acceptable — the single FIFO worker drains them sequentially over time
  (optionally capped per loop iteration to smooth load).
- A **single** worker (dedicated 1-slot executor, guarded by a Postgres
  advisory lock) pulls the oldest `queued` run and executes §11. The advisory
  lock guarantees "never two at once" even across accidental double-workers.
- Manual "run now" inserts a `queued` run (or runs immediately if idle).
- Queue is DB-persisted → survives restart; startup reconciles stuck runs.
- **Retry:** a `failed`/`error` run re-enqueues after `restore_retry_days`
  (default **7**, per-VM adjustable). (`passed`/`limited` count as a completed
  test and simply wait for the next cadence date.) The operator can also set `next_run_at`
  explicitly to reschedule the next run to any time. (Optional small cap on
  consecutive auto-retries before it falls back to the normal cadence, so a
  persistently broken VM doesn't spin the queue forever.)
- **Hold on failure:** if a VM has `hold_on_failure` set and a run ends
  `failed`/`error`, the run ends `held` (§11): the environment is **parked** (tracked, VMID/tag/
  mgmt-IP retained) and the active slot is released, so **the queue keeps
  running**. Multiple held environments may coexist, bounded only by host RAM/
  disk. The operator reaps each held environment **individually** from the UI
  ("Abräumen"); reaping schedules that VM's retry (`restore_retry_days`).
- **Re-test a held environment:** the helper (with its probe agent) stays up, so
  after the operator fixes the guest or adjusts the probe config (ports,
  primary-IP, require-ICMP), an **"erneut testen"** action re-runs the probe loop
  against the **standing** VM — no new restore, no reboot — and appends the
  attempt to the run's `checks`/`log`. It does **not** consume the active-test
  slot (it is just probing, not restoring).
- **Promote on green:** if a re-test turns `passed`, the system **asks** the
  operator whether to **document it as a successful restore and reap**. On yes:
  write the `passed` `restore_tests` row (tested_at = now → due clock reset),
  mark the run `passed`, reap the VM **and** its helper, free the VMID/tag/
  mgmt-IP, and reschedule by the normal cadence. On no: the environment stays
  `held` for further fixing.
- **Manual queue control:** the operator can **pause/resume** the whole queue at
  any time (the control for "RAM/disk getting tight, stop starting new ones").
  Default is running; pausing never touches existing environments.
- **Resource-aware skip (RAM/disk, no stop):** before starting a run, the worker
  estimates whether it fits alongside current held environments (RAM/disk). If it
  doesn't fit **right now**, the worker **skips that VM and moves on** to the next
  due VM that does fit; the skipped VM stays due and is retried on later passes.
  The queue just keeps going relaxedly — big VMs naturally wait until holds are
  reaped and space frees up. No automatic pause (that stays a manual lever).
- **Management-IP exhaustion → wait:** every run needs a free static management
  IP (§6). If none is free (all consumed by the active run + held environments),
  **no run can start, so the queue waits** until a held environment is reaped and
  an IP frees. (Distinct from the RAM/disk skip: here nothing can start at all.)
- **Alerting:** `failed`/`error`/`held` runs raise a notification through the
  existing channels (`app/services/notifications.py`), since catching a broken
  restore is the whole point. (A VM that stays un-fittable well past its due
  date may get a soft notice; skipping itself is silent and normal.)

## 16. UI (server-rendered, de/en, Smile Eyes styling)

- **Settings:** a "Restore-Server / PVE-Restore" section — credentials, SSH key,
  target storage picker, sandbox bridge name + optional VLAN, time-budget
  defaults, a "test connection + ensure sandbox bridge" action.
- **Per-VM (governance/cluster view):** toggle auto-restore, cadence select,
  primary-IP override, TCP ports, require-ICMP toggle, time-budget overrides,
  **"Jetzt testen"** button, and the last automated result. VMs whose 3-tuple
  the scratch host does not see are marked **"not auto-restorable (no offsite
  backup)"** and the toggle is disabled for them.
- **Restore runs view:** queue + history, status, duration, the full log, and
  the structured checks; links from the governance restore column. A **running**
  restore shows **live percent + ETA + throughput**. A **host panel** shows the
  PVE-Restore's **free RAM** and **free disk space per store** (live), so the
  operator can judge capacity. A **held environments** panel lists each held VM
  (how to reach it: VMID, VLAN tag, helper mgmt-IP) with its own **"erneut
  testen"** and **"Abräumen"** buttons, and — when a re-test is green — a
  **confirm prompt** "als erfolgreichen Restore dokumentieren und abräumen?". A
  **pause/resume queue** control. Per-VM: the hold-on-failure toggle,
  `restore_retry_days`, and a manual "next run at …".
- Audit via `append_audit_event` under the `restore_test.`/`restore_server.`
  namespaces for config changes and run lifecycle.

## 17. Security & safety summary

- All destructive/SSH actions only on the dedicated scratch host; production
  stays read-only (IP collection is read-only via the agent).
- Portless bridge ⇒ sandbox cannot reach production or any switch. The helper is
  dual-homed (sandbox + management) but never forwards/routes between the two
  (`ip_forward=0`, no NAT), so the restored VM cannot escape via the helper.
  The probe-agent endpoint is reachable only on the management leg and
  per-run-token authenticated.
- Reaper + host-identity interlock + target-storage interlock fail safe.
- SSH key and API token Fernet-encrypted; never logged. SSH host key pinned.
- Feature inert until configured and per-VM enabled; disabled by default.

## 18. Open calls made by the designer (not asked)

- Sandbox bridge ifname defaults to `vmbr9` (configurable), UI label
  "Restore Sandbox", because a Linux bridge name can't contain spaces/>15 chars.
- Cadence→days mapping 30/91/182/365; half-year default.
- `auto-full-vm` as the `recovery_type` string for automated runs.
- Single restore server in v1; schema left extensible.
- `error` is a distinct `restore_runs` status, treated like `failed` for
  scheduling/alerting; neither writes a `restore_tests` row.

## 19. Left to the builder / plan

- Exact PVE API calls & task-log polling for restore/start/stop/destroy, and
  the SSH command set for bridge-ensure, LXC+dnsmasq, and the sweep.
- Snapshot-completeness criterion against the PBS API.
- Helper LXC image/template provisioning strategy, and the **probe-agent
  contract** (invocation + the structured-JSON result schema) so socket/service
  checks can grow without changing the worker.
- Table DDL, migrations, row types (`app/types.py`), and query placement across
  `db_*` modules.
- Worker/executor wiring into `auto_sync_loop` and the advisory-lock helper.
- Template/i18n/styling details and tests (TDD per the project's workflow).
