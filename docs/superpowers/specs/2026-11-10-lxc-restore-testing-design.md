# Design: Restore testing for LXC containers

**Date:** 2026-11-10
**Status:** Approved direction, pending document sign-off
**Author:** brainstormed with Wolf Vogel

## Intent and why

Backup Sentinel's restore test proves a backup is actually restorable: it
restores a guest into an isolated sandbox, boots it, and checks it is reachable
on the network. Today this works for **QEMU VMs only** — LXC containers are
cleanly excluded with `NotRunnable("containers (LXC) are not supported yet — VM
restore only")` ([restore_orchestrator.py:283](../../../app/services/restore_orchestrator.py)).

That leaves an entire guest class whose backups are never restore-verified. The
immediate case is a **Corosync Qdevice** running as an LXC (the `corosync-qnetd`
daemon, listening on **TCP 5403**), but the real motivation is forward-looking:
when ordinary service containers arrive (a reverse proxy, a small app, a DB in an
LXC), they should be covered from day one instead of silently falling through the
gate. The operator considers LXC a first-class guest type and wants parity with
VMs.

The key finding from design exploration: the **probing and classification layer
is 100% reusable**. A container restore test needs to prove exactly what a VM
test proves — liveness (ICMP or ARP) plus configured TCP ports accepting a
**socket** connection (deliberately socket-level only, no protocol handshake).
That is already what `restore_probe` / the helper agent do. So this work is
*not* a second probing system; it is only a second **path to a running guest**.

## Goals

- Run the full automated restore test for LXC guests: restore from PBS into the
  sandbox, boot, probe, classify (passed / limited / failed), tear down.
- Reuse the existing probe/classification, queue, scheduling, sandbox bridge,
  helper, per-VM policy (`probe_tcp_ports`, `probe_primary_ip`, cadence, hold),
  UI (runs table, live card, phase timings), benchmark and teardown unchanged.
- Make the Qdevice LXC a working first case: operator sets `probe_tcp_ports =
  [5403]`, the test proves `corosync-qnetd` answers after restore.
- IP discovery without a guest agent (containers have none): static `ip=` from
  the container's `netN` config, DHCP fallback via the sandbox dnsmasq + ARP.

## Non-goals (v1)

- **Per-disk distribution across storages (Stufe 2) for containers.** That logic
  is QEMU-keyed (scsi/virtio/sata disk keys). v1 restores a container onto **one
  target storage**; the ordered single-storage fallback still applies.
- Protocol-level checks (HTTP status, TLS, app health). Socket connect only, as
  for VMs.
- Reconstructing host bind-mounts (see anti-goals).
- Restoring from vzdump archive files (`.tar.zst` on a dir/NFS storage) — v1
  targets **PBS-sourced** container backups, the same source VMs use. (If the
  builder finds vzdump-file restore is trivially the same API call, it may
  include it, but it is not required.)

## Anti-goals (what would make this a failure even if it "works")

- **Touching production.** The fail-closed route guard, host-identity check and
  reap must apply to the container path exactly as to VMs. A container restore
  must never boot on, probe, or reach the real production container (same vmid /
  MAC / IP concern as VMs).
- **Reconstructing host bind-mounts.** A container `mpN` that points at a host
  path (bind mount) is not in the backup and must not be recreated or pointed at
  a host path in the sandbox. Such mounts are **neutralized** (dropped) before
  boot so the container starts; volume-backed mountpoints that ARE in the backup
  restore normally.
- **A half-working path that reports false greens/reds.** Better to keep the
  clean exclusion than to ship a container path that misclassifies. The guard is
  only lifted once the path is proven against a real host.

## Constraints

- Containers have **no QEMU guest agent** → IP cannot come from
  `agent/network-get-interfaces`. It must come from config (`ip=`) or sandbox
  DHCP.
- `pct restore` / the LXC restore API, not `qmrestore`. Restore as **root**
  (same as VMs: a token can't do everything), background task + UPID poll for
  live progress and re-attach after a deploy.
- LXC net config format differs from QEMU (`net0: name=eth0,bridge=...,hwaddr=...,
  ip=...,gw=...,tag=...`) → a container-specific net rewrite.
- The restore host already runs its helper as an LXC, so LXC tooling exists
  (`exists(..., is_lxc=True)`, `is_running(is_lxc=True)`, `vm_locked(is_lxc=True)`,
  `start_lxc`, `destroy(..., is_lxc=True)`, `all_vmids` covers qemu+lxc).

## What is reused as-is (the bulk of the system)

- **Probe + classify:** `restore_probe.classify_multi`, `green_check_labels`,
  the helper agent's parallel ICMP/ARP/TCP probing, the `_probe_loop` cadence.
  ARP liveness works for a container on the sandbox L2 just like a VM.
- **Orchestration shell:** the queue/drainer, watchdog, `execute()` pipeline and
  its phases (`setup → preflight → helper → restoring → network → booting →
  probing`), phase timings, teardown, retry/hold, benchmark interlock.
- **Sandbox + helper:** bridge, VLAN allocation, dnsmasq DHCP, management leg,
  the fail-closed `_route_via_sbx` guard.
- **Per-VM policy & UI:** `probe_tcp_ports`, `probe_primary_ip`, cadence, hold,
  the runs table / live card / settings — all keyed on the VM, type-agnostic.

## The decided "how" — seams and changes

The pipeline stays the same; the guest-type-specific steps branch on
`guest_type`. Concretely:

1. **Lift the guard.** In `restore_orchestrator` remove/relax the two LXC
   exclusions ([:283-284](../../../app/services/restore_orchestrator.py) build_plan,
   [:422-425](../../../app/services/restore_orchestrator.py) candidate scan) so
   `guest_type == 'lxc'` becomes runnable.

2. **Restore (`RestoreServer.restore`).** Branch on guest type (plumb the type
   into the plan/call). For LXC: start a background **`pct restore`** task
   (`pvesh create /nodes/{node}/lxc` with `-ostemplate <pbs-archive-volid>`,
   `-vmid`, `-storage <target>`, root, async) returning a UPID, then reuse
   `_poll_task` for live progress + `on_task(upid)`. The PBS archive volid
   resolution (`_archive_volid`) is shared. No `_restore_distributed` for LXC in
   v1 (single storage).

3. **Net rewrite for LXC** (parallel to `rewrite_nics`). In one config update on
   `/nodes/{node}/lxc/{vmid}/config`: rename to `<cluster>-<vm>`; for each `netN`
   re-point `bridge=` to the sandbox bridge and set `tag=` to this env's VLAN
   (keep `hwaddr=`); keep a static `ip=`/drop `gw=` reachability as needed for an
   isolated boot; neutralize host bind-mount `mpN` entries; LXC has no
   `hostpciN`/CD-ROM, so those branches are N/A. A container-specific
   `rewrite_lxc_net_line` pure helper mirrors `rewrite_net_line`.

4. **Boot.** Use the existing `start_lxc` (and `wait_unlocked(is_lxc=True)`)
   instead of `start`. `exists`/`is_running`/`destroy`/reap already take
   `is_lxc`.

5. **IP discovery** (the one genuinely new probe-side piece). In
   `restore_ip_collect` the LXC branch currently returns no agent NICs
   ([:82-83](../../../app/services/restore_ip_collect.py)). Add: parse `ip=` (CIDR)
   and `hwaddr=` from each `netN` of the LXC config → a NIC row with
   `source="config"`. `ip=dhcp` falls back to the sandbox-DHCP primary IP exactly
   as the VM no-agent path does. `probe_primary_ip` override still wins.

   **Decided seam (architect's call):** the static-IP path must feed the SAME
   runtime per-pass mechanism VMs use — an **LXC equivalent of
   `guest_agent_nics(vmid)`** that, each probe pass, reads the container's live
   `netN` config and returns `[(ip, prefix)]`. `_probe_loop` then treats it like
   the VM's discovered-IP list: the helper pins a host in the container's **real
   subnet/prefix** at probe time (`_ensure_reachable`), which is what makes the
   port actually reachable. Do **not** merely set `plan.primary_ip` from the
   config `ip=` at build time: in the no-override branch `_probe_loop` would then
   probe `plan.primary_ip` at `plan.sandbox_prefix` (the management-fallback
   prefix, not the container's), which only works if the prefixes happen to
   coincide and otherwise yields a false limited/failed. With this seam the
   static-IP case (the Qdevice) works **without** requiring a `probe_primary_ip`
   override; an override, if set, still pins `_sandbox_alloc` the strict way.

6. **Classification & checks:** unchanged. `probe_tcp_ports` drives the port
   checks; a container with ports configured is judged like a VM; one without is
   liveness-only (ICMP/ARP) — but the Qdevice has 5403, so it is a real port
   test.

### Target-storage capability (in scope — operator confirmed container-only storages exist)

The operator has storages that are **container-capable (`content=rootdir`) but
not image-capable (`content=images`)**. The whole target-storage layer is keyed
on image capability today, so this is a real, in-scope change, not a reuse:

- **Selection/validation by content type.** For an LXC run the target must have
  `content=rootdir`; for a VM run, `content=images` (as today). `_lxc_storage`
  ([restore_server.py:1759](../../../app/services/restore_server.py)) already picks
  the first selected target whose content includes `rootdir` — reuse that logic
  for choosing/validating a container's target, and raise a clear error if none
  of the selected targets can hold a container.
- **Settings target-storage UI + `ui_target_storages`**
  ([restore_orchestrator.py:692](../../../app/services/restore_orchestrator.py))
  **must surface rootdir-capable storages**, not only image-capable ones, so the
  operator can tick a container-only storage. A storage may be image-only,
  container-only, or both; the UI should make the capability visible.
- **`choose_target_storage` / `_fits_disk` / `free_resources`** operate on the
  type-appropriate selected set.
- **`restore_total_bytes`** ([restore_server.py:1686](../../../app/services/restore_server.py))
  reads `content=images&vmid=` to size progress; for a container it must size
  from the rootdir/mountpoint content instead, degrading to raw task percent if
  unknown (as the VM path already does).

## Defaults decided (operator approved)

- **One target storage per container** in v1 (no disk distribution).
- **Bind-mounts neutralized, not reconstructed.**
- **IP discovery: static `ip=` first, DHCP fallback.**
- **Target storage chosen/validated by `rootdir` capability for containers; the
  storage-selection UI surfaces rootdir-capable storages** (operator has
  container-only storages).

## Edge cases / builder considerations

- **Unprivileged vs privileged / nesting / features:** `pct restore` restores the
  config as-is; most containers start in the sandbox unchanged. Note any feature
  that needs host resources the scratch host lacks; prefer letting the start fail
  and be reported as `failed` over silently editing the container.
- **`rootfs` + volume `mpN`** restore normally from PBS; only host **bind** `mpN`
  are neutralized. The builder must distinguish bind mounts (host path) from
  volume mounts (storage-backed) when neutralizing.
- **Progress parsing:** `pct restore`'s task log format differs from
  `pbs-restore`'s per-disk lines; the overall-progress updater
  (`_progress_updater` / `restore_total_bytes`) may need a container-aware total
  (rootfs + volume mountpoint sizes) — degrade gracefully to raw task percent if
  unknown, as the VM path already does.
- **Qdevice specifics:** restoring a qnetd into the sandbox proves the daemon
  boots and listens; it will not (and need not) join the real cluster. Probe
  port 5403 only.

## Testing strategy

- **Pure, no-DB tests** for every new pure helper: `rewrite_lxc_net_line`
  (bridge/tag/hwaddr/ip/gw rewrite, bind-mount neutralization), the LXC `ip=` /
  `hwaddr=` config parser, and any container progress-total helper. TDD.
- **DB-gated tests** (`BSENTINEL_DB_TESTS=1`, run in CI): candidate selection now
  includes an LXC guest; `execute()` over a `FakeServer` drives the LXC branch to
  `passed` and records phase timings — mirror the existing QEMU `WorkerTest`.
- **Real-host verification** is required before the guard is lifted in a release:
  the Qdevice LXC restored, booted and 5403 green on the actual restore host. The
  `NotRunnable` guard stays until this passes (anti-goal: no false greens).
  **The Qdevice is backed up and sourceable from the restore host (operator
  confirmed), so this verification subject exists** — the green run is the final
  step of the implementation, and the guard is lifted in the same release once it
  passes. No merge-behind-guard fallback is needed.

## What's left to the builder

- Exact `pct restore` / `pvesh` invocation and flags, and the container progress
  parsing, confirmed against a live PVE/PBS host.
- Whether the guest-type branch is best expressed as a small strategy split
  inside `RestoreServer` (two private `_restore_qemu` / `_restore_lxc` + two net
  rewrites) or a thin type dispatch — builder's call, kept minimal and testable.
- The precise bind-mount neutralization rule (drop the `mpN` vs. replace with a
  throwaway sandbox volume) — default: drop, unless the builder finds dropping
  prevents boot for a common config.
- Plumbing `guest_type` from the plan into `restore()` and the net-rewrite call.

## Success

A scheduled/manual restore test of an LXC (starting with the Qdevice) runs end to
end on the real host: restored from PBS, booted in the sandbox, 5403 answering,
classified `passed`, torn down — shown in the same runs table and live card as a
VM, with phase timings. No regression to the VM path, and production is never
touched.
