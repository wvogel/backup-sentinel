# LXC Restore Testing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the automated restore test to LXC containers — restore from PBS into the sandbox, boot, probe, classify — reusing the entire probe/queue/UI layer and adding only the container-specific path.

**Architecture:** The existing pipeline (`execute()` phases, `_probe_loop`, teardown) stays; guest-type-specific steps branch on `guest_type`. New pure helpers parse/rewrite LXC `netN` config and neutralize bind-mounts; `RestoreServer.restore` gets a `pct restore` branch; IP discovery reads the container config each probe pass (no guest agent); the target-storage layer learns `rootdir` vs `images` capability.

**Tech Stack:** Python 3.12, FastAPI, psycopg (PostgreSQL), Proxmox VE/PBS APIs via the existing `RestoreServer` SSH/token clients, pytest.

**Spec:** [docs/superpowers/specs/2026-11-10-lxc-restore-testing-design.md](../specs/2026-11-10-lxc-restore-testing-design.md)

## Global Constraints

- Python target `py312`; `ruff check` and `ruff format --check` over `app tests scripts` must pass (run verbatim).
- Pure-logic tests run always; DB-touching tests are gated behind `BSENTINEL_DB_TESTS=1` and TRUNCATE tables (mirror `tests/test_db.py` / `tests/test_restore_worker.py` style with `setUpModule → db.init_db()`).
- Socket-level port checks only — no protocol handshake (reuse `restore_probe`, unchanged).
- Never touch production: the fail-closed `_route_via_sbx` guard, host-identity check and reap apply to the LXC path exactly as to VMs. Do not weaken them.
- One target storage per container (no `_restore_distributed` for LXC in v1).
- Restore as root in the background, return the task UPID, poll it (re-attach after a deploy) — same contract as the VM path.

## Review Focus

- **Static-IP container in a subnet whose prefix differs from the management fallback** → the probe must reach it in its *own* subnet, not at `sandbox_prefix`; otherwise a false `limited/failed`. Covered by Task 3 tests (`lxc_config_nics` returns real `(ip, prefix)`) and Task 7 (probe loop uses it).
- **`ip=dhcp` container** → must fall back to the sandbox-DHCP primary IP like a VM with no agent; no static NIC row emitted. Covered by Task 1 tests.
- **Bind-mount `mpN` pointing at a host path** → must be dropped, not restored or pointed at a host path, or the container fails to boot in the sandbox. Covered by Task 2 tests.
- **A selected target storage that is image-only (no `rootdir`)** → a container restore must refuse it with a clear error, never attempt `pct restore` onto it. Covered by Task 4 tests.
- **Container-only (`rootdir`, no `images`) storage** → must be selectable in the UI for containers and never offered for VMs. Covered by Task 4 tests.

---

## Task 1: Parse LXC `netN` config (pure)

**Files:**
- Create: `app/services/restore_lxc.py`
- Test: `tests/test_restore_lxc.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `parse_lxc_net(value: str) -> dict` → `{"mac": str|None, "ip": str|None, "prefix": int|None}`. `ip`/`prefix` are set only for a static CIDR `ip=` (not `dhcp`/`manual`/missing).
  - `lxc_config_nics(config: dict) -> list[tuple[str, int]]` → `[(ip, prefix), …]`, one per `netN` with a static CIDR, ordered by net index.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_static_cidr():
    v = "name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:DD:EE:FF,ip=10.0.0.5/24,gw=10.0.0.1,tag=5"
    assert parse_lxc_net(v) == {"mac": "aa:bb:cc:dd:ee:ff", "ip": "10.0.0.5", "prefix": 24}

def test_parse_dhcp_has_no_ip():
    v = "name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:DD:EE:FF,ip=dhcp"
    assert parse_lxc_net(v) == {"mac": "aa:bb:cc:dd:ee:ff", "ip": None, "prefix": None}

def test_config_nics_orders_and_skips_non_static():
    cfg = {
        "net0": "name=eth0,hwaddr=AA:AA:AA:AA:AA:AA,ip=192.168.1.9/26,gw=192.168.1.1",
        "net1": "name=eth1,hwaddr=BB:BB:BB:BB:BB:BB,ip=dhcp",
    }
    assert lxc_config_nics(cfg) == [("192.168.1.9", 26)]
```

- [ ] **Step 2: Run to verify they fail** — `pytest tests/test_restore_lxc.py -v` → FAIL (module/functions not defined).
- [ ] **Step 3: Implement `parse_lxc_net` and `lxc_config_nics` in `app/services/restore_lxc.py`.** Split the comma list into `k=v`; reuse the MAC regex style from `restore_ip_collect` (lowercase); treat only a `value/prefix` CIDR as static.
- [ ] **Step 4: Run to verify they pass** — `pytest tests/test_restore_lxc.py -v` → PASS.
- [ ] **Step 5: Commit** — `git add app/services/restore_lxc.py tests/test_restore_lxc.py && git commit -m "feat(restore): parse LXC netN config (ip/prefix/mac)"`.

---

## Task 2: Rewrite LXC net for the sandbox + neutralize bind-mounts (pure)

**Files:**
- Modify: `app/services/restore_lxc.py`
- Test: `tests/test_restore_lxc.py`

**Interfaces:**
- Consumes: Task 1 module.
- Produces:
  - `rewrite_lxc_net_line(value: str, bridge: str, tag: int, trunks: int | None = None) -> str` — repoints `bridge=` to `bridge`, sets `tag=` to `tag`, keeps `hwaddr`/`name`/`ip`/`gw`; adds a `trunks=` when `trunks` is set (mirrors `rewrite_net_line` in restore_server.py:231).
  - `bind_mount_deletes(config: dict) -> list[str]` — the `mpN` keys that are **host bind-mounts** (the mount source is a host path, i.e. not `storage:volid`), to be deleted before boot. Volume-backed `mpN` (source `storage:volid`) are kept.

- [ ] **Step 1: Write the failing tests**

```python
def test_rewrite_keeps_hwaddr_and_ip_repoints_bridge_and_tag():
    v = "name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:DD:EE:FF,ip=10.0.0.5/24,gw=10.0.0.1,tag=7"
    out = rewrite_lxc_net_line(v, "vmbr9", 901)
    assert "bridge=vmbr9" in out and "tag=901" in out
    assert "hwaddr=AA:BB:CC:DD:EE:FF" in out and "ip=10.0.0.5/24" in out

def test_rewrite_adds_trunk_for_internally_tagged_guest():
    out = rewrite_lxc_net_line("name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:DD:EE:FF", "vmbr9", 901, trunks=1010)
    assert "trunks=1010" in out

def test_bind_mounts_are_deleted_volume_mounts_kept():
    cfg = {
        "mp0": "/srv/host-data,mp=/data",                 # host bind-mount → delete
        "mp1": "local-lvm:vm-123-disk-1,mp=/var/lib/db",  # volume mount → keep
        "rootfs": "local-lvm:vm-123-disk-0,size=8G",
    }
    assert bind_mount_deletes(cfg) == ["mp0"]
```

- [ ] **Step 2: Run to verify they fail** — `pytest tests/test_restore_lxc.py -v` → FAIL.
- [ ] **Step 3: Implement both in `app/services/restore_lxc.py`.** A mount source with a leading `/` (no `storage:` prefix) is a bind-mount.
- [ ] **Step 4: Run to verify they pass** — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(restore): LXC sandbox net rewrite + bind-mount neutralization"`.

---

## Task 3: Supported guest types + `(ip, prefix)` discovery feeding the probe loop

**Files:**
- Modify: `app/services/restore_orchestrator.py` (guards at ~283 and ~425), `app/services/restore_worker.py` (`RunPlan`)
- Modify: `app/services/restore_server.py` (`lxc_config_nics(vmid)` method) and/or `app/services/restore_lxc.py`
- Test: `tests/test_restore_lxc.py`, `tests/test_restore_worker.py`

**Interfaces:**
- Consumes: Task 1 `lxc_config_nics`.
- Produces:
  - Module constant `SUPPORTED_GUEST_TYPES = {"qemu", "lxc"}` and `is_supported_guest(guest_type: str | None) -> bool` in `restore_orchestrator.py`.
  - `RunPlan.guest_type: str = "qemu"` field.
  - `RestoreServer.lxc_nics(self, vmid: int) -> list[tuple[str, int]]` — reads `/nodes/{node}/lxc/{vmid}/config` and returns `lxc_config_nics(config)` (the pure helper from Task 1); the probe loop calls this (for an LXC run) where it calls `guest_agent_nics` for a VM. (Method name differs from the pure helper to avoid a collision.)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_restore_lxc.py
def test_is_supported_guest():
    assert is_supported_guest("qemu") and is_supported_guest("lxc")
    assert not is_supported_guest("other")
```
Plus, in `tests/test_restore_worker.py` (DB-gated class), extend the plan factory to accept `guest_type` and assert `RunPlan(guest_type="lxc").guest_type == "lxc"`.

- [ ] **Step 2: Run to verify they fail** — FAIL (`is_supported_guest` undefined; `guest_type` not a field).
- [ ] **Step 3: Implement.** Add the constant/predicate; replace both `(candidate.get("guest_type") or "qemu") != "qemu"` guards so only `not is_supported_guest(...)` raises/skips. Add the `RunPlan.guest_type` field and set it in `build_plan` (from `candidate["guest_type"]`) and `plan_from_run` (from `run["guest_type"]`). Add `RestoreServer.lxc_config_nics`.
- [ ] **Step 4: Run to verify they pass** — PASS (pure test always; worker field test under `BSENTINEL_DB_TESTS=1`).
- [ ] **Step 5: Commit** — `git commit -m "feat(restore): accept LXC guests; carry guest_type; LXC config NIC discovery"`.

---

## Task 4: Target-storage capability — `rootdir` vs `images`

**Files:**
- Modify: `app/services/restore_orchestrator.py` (`ui_target_storages` ~650, `choose_target_storage` ~233, `_fits_disk` ~433)
- Modify: `app/services/restore_server.py` (generalize `_lxc_storage` ~1759)
- Test: `tests/test_restore_target_choice.py`

**Interfaces:**
- Consumes: `RunPlan.guest_type` (Task 3).
- Produces:
  - `storage_accepts_guest(content: set[str] | list[str], guest_type: str) -> bool` (pure, in `restore_orchestrator.py`): `rootdir` for lxc, `images` for qemu.
  - `ui_target_storages()` returns, per storage, its capability flags (`images`, `rootdir`) so the Settings UI can show and the operator can tick a container-only storage.
  - `choose_target_storage` / the selected-set filter respect `guest_type` (a container never lands on an image-only storage; a VM never on a container-only one).

- [ ] **Step 1: Write the failing tests**

```python
def test_storage_accepts_guest_by_content():
    assert storage_accepts_guest({"images", "rootdir"}, "lxc")
    assert storage_accepts_guest({"rootdir"}, "lxc")
    assert not storage_accepts_guest({"images"}, "lxc")
    assert not storage_accepts_guest({"rootdir"}, "qemu")

def test_choose_target_skips_wrong_capability():
    # an lxc plan over storages [img-only, rootdir-capable] picks the rootdir one
    ...  # assert choose_target_storage(...) == "ct-store"
```

- [ ] **Step 2: Run to verify they fail** — FAIL.
- [ ] **Step 3: Implement `storage_accepts_guest`; thread `guest_type` into `choose_target_storage`/`_fits_disk`; have `ui_target_storages` include capability flags and no longer hard-drop non-`images` storages (surface both capabilities).** Reuse the `rootdir` logic already in `_lxc_storage`.
- [ ] **Step 4: Run to verify they pass** — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(restore): target-storage capability (rootdir vs images) for containers"`.

---

## Task 5: `RestoreServer.restore` — `pct restore` branch + config rewrite + boot

**Files:**
- Modify: `app/services/restore_server.py` (`restore` ~1048, `_start_restore_task` ~1317, `rewrite_nics` ~1579), `app/services/restore_worker.py` (`execute` boot step)
- Test: `tests/test_restore_server.py`, `tests/test_restore_worker.py`

**Interfaces:**
- Consumes: `RunPlan.guest_type`, Task 2 (`rewrite_lxc_net_line`, `bind_mount_deletes`), Task 3 (`lxc_config_nics`).
- Produces:
  - `restore(...)` branches on `guest_type` (plumbed via the caller): LXC → `_start_lxc_restore_task(restore_vmid, archive, target_storage, node, …) -> upid` (`pvesh create /nodes/{node}/lxc -ostemplate <archive> -vmid <id> -storage <target>`, root, async), then reuse `_poll_task`. VM path unchanged.
  - `rewrite_config_for_sandbox(vmid, vlan_tag, guest_type, guest_vlan=None, name="")` — dispatches: qemu → current `rewrite_nics`; lxc → rewrite `netN` via `rewrite_lxc_net_line`, delete `bind_mount_deletes`, rename, on `/nodes/{node}/lxc/{vmid}/config` (no hostpci/CD-ROM).
  - `execute()` boots via `start_lxc` + `wait_unlocked(..., is_lxc=True)` when `guest_type == "lxc"`.

- [ ] **Step 1: Write the failing tests** (unit, with a fake pve/ssh client — mirror existing `tests/test_restore_server.py`):

```python
def test_restore_dispatches_to_lxc_task_for_lxc_guest():
    # a fake server records which task-starter ran; guest_type='lxc' → _start_lxc_restore_task called
    ...

def test_rewrite_config_lxc_repoints_net_and_drops_bind_mount():
    # fake config get returns net0 + mp0 bind-mount; assert the posted changes
    # contain bridge=<sandbox>, tag=<vlan>, and delete mp0
    ...
```

- [ ] **Step 2: Run to verify they fail** — FAIL.
- [ ] **Step 3: Implement the branch, `_start_lxc_restore_task`, and `rewrite_config_for_sandbox`.** The exact `pvesh`/`pct restore` flags are builder-confirmed on a live host (Task 8); keep the SSH/token split as the VM path does (root for the restore).
- [ ] **Step 4: Run to verify they pass** — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(restore): pct-restore path, LXC sandbox config rewrite, LXC boot"`.

---

## Task 6: Container progress sizing

**Files:**
- Modify: `app/services/restore_server.py` (`restore_total_bytes` ~1686)
- Test: `tests/test_restore_server.py`

**Interfaces:**
- Consumes: `RunPlan.guest_type`.
- Produces: `restore_total_bytes(vmid, guest_type="qemu")` — for lxc, size from the container's `rootdir`+volume-`mpN` content (`content=rootdir`) instead of `content=images`; return 0 when unknown (caller already degrades to raw task percent).

- [ ] **Step 1: Write the failing test** — fake storage-content response for a container returns the summed rootfs+mp size; unknown → 0.
- [ ] **Step 2: Run to verify it fails** — FAIL.
- [ ] **Step 3: Implement the lxc sizing branch** (degrade gracefully, as the VM path does on error).
- [ ] **Step 4: Run to verify it passes** — PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(restore): size container restore progress from rootdir content"`.

---

## Task 7: End-to-end LXC run reaches `passed` (DB-gated)

**Files:**
- Modify: `app/services/restore_worker.py` (`_probe_loop` LXC NIC lookup; ensure `execute` uses guest_type for boot/rewrite/restore)
- Test: `tests/test_restore_worker.py`

**Interfaces:**
- Consumes: Tasks 3, 5, 6.
- Produces: in `_probe_loop`, when `plan.guest_type == "lxc"` and no override, resolve live NICs via `server.lxc_nics(plan.restore_vmid)` (returning `[(ip, prefix)]`) in place of `guest_agent_nics`; the rest of the loop is unchanged (the helper pins the container's real subnet via the per-target `prefix`).

- [ ] **Step 1: Write the failing test** (DB-gated, mirror `test_passed_writes_test_row_and_tears_down`): a `FakeServer` whose `lxc_config_nics` returns `[("10.0.0.50", 24)]` and that records `start_lxc`; an `execute()` over an **lxc** plan → returns `"passed"`, writes the restore-test row, records phase timings, and used the LXC boot (`start_lxc` in `srv.calls`).
- [ ] **Step 2: Run to verify it fails** — `BSENTINEL_DB_TESTS=1 pytest tests/test_restore_worker.py -k lxc -v` → FAIL.
- [ ] **Step 3: Implement the `_probe_loop` LXC branch and any remaining `execute()` guest_type dispatch.**
- [ ] **Step 4: Run to verify it passes** — PASS (under `BSENTINEL_DB_TESTS=1`).
- [ ] **Step 5: Commit** — `git commit -m "feat(restore): drive a full LXC restore test to passed"`.

---

## Task 8: Real-host verification + release (manual)

**Files:**
- Modify: `CHANGELOG.md`, `app/config.py` (version bump)
- Modify (docs): `README.md`, `docs/admin-docs-de.md`, `docs/admin-docs-en.md` (LXC now tested)

- [ ] **Step 1:** Full local verification — `ruff check app tests scripts`, `ruff format --check app tests scripts`, pure suite green, and the DB-gated suite green against a fresh Postgres.
- [ ] **Step 2:** On the real restore host, run a restore test of the **Qdevice LXC**: confirm it restores from PBS, boots in the sandbox, and **TCP 5403** is green → classified `passed`, then torn down. (Set `probe_tcp_ports=[5403]` on the Qdevice first.)
- [ ] **Step 3:** If green, bump the version + changelog (`Added: LXC container restore tests`), update the docs, commit.
- [ ] **Step 4:** Release via the normal flow (push to both remotes, watch CI). The `NotRunnable` guard is already removed (Task 3); the on-host green run is the gate that it was safe to remove.

---

## Notes for the executor

- Work on a feature branch / worktree (not `main`).
- Keep the VM path untouched in behavior — every change is additive or a type-dispatch; the existing VM worker/server tests must stay green throughout.
- The exact `pct restore`/`pvesh` invocation, flags, and container task-log progress parsing are confirmed against the live host in Task 8; until then Task 5's tests pin the dispatch and config-rewrite, not the on-host command.
