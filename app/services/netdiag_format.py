"""Human-readable rendering of the restore probe's network diagnostics.

``RestoreServer.probe_netdiag`` collects raw command output as ``(label, value)``
pairs (``value`` is the stdout, or ``"ERR …"`` when the command failed). This
module turns them into a short, sectioned report for the run log — bridge,
restored VM NIC, helper sandbox leg, routes, then one block per guest IP with
ICMP / ARP / each TCP port.

Runs recorded before this existed stored the pairs on ONE line
(``probe netdiag [label: value || …]``, newlines flattened); ``format_run_log``
re-parses that so old logs read the same way."""

from __future__ import annotations

import re

LEGACY_PREFIX = "probe netdiag ["
_IP_RE = re.compile(r"^(ping|neigh) (\S+)$")
_TCP_RE = re.compile(r"^tcp (\S+):(\d+)$")
_VLAN_FLAGS = {"PVID", "Egress", "Untagged"}


def _is_err(value: str) -> bool:
    return value.startswith("ERR")


def _vid_list(vids: list[tuple[int, int, list[str]]]) -> str:
    """``[(2, 2, []), (3, 3, []), (1, 1, ['PVID', …])]`` → ``1 PVID untagged, 2–3``."""
    vids = sorted(vids)
    out: list[str] = []
    for lo, hi, flags in vids:
        flagged = bool(flags)
        if out and not flagged and not out[-1][2] and out[-1][1] + 1 == lo:
            out[-1] = (out[-1][0], hi, [])
        else:
            out.append((lo, hi, flags))
    parts = []
    for lo, hi, flags in out:
        text = str(lo) if lo == hi else f"{lo}–{hi}"
        tags = []
        if "PVID" in flags:
            tags.append("PVID")
        if "Untagged" in flags:
            tags.append("untagged")
        parts.append(f"{text} ({', '.join(tags)})" if tags else text)
    return ", ".join(parts)


def _parse_vlans(value: str) -> dict[str, list[tuple[int, int, list[str]]]]:
    """``bridge [-c] vlan show`` output (multi-line, or flattened to one line) →
    ``{port: [(lo, hi, flags)]}``. A non-numeric, non-flag token starts a port."""
    ports: dict[str, list[tuple[int, int, list[str]]]] = {}
    current: str | None = None
    for tok in value.split():
        if tok in ("port", "vlan-id", "vlan", "ids"):
            continue
        if tok in _VLAN_FLAGS:
            if current and ports[current]:
                ports[current][-1][2].append(tok)
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", tok)
        if m and current:
            lo = int(m.group(1))
            ports[current].append((lo, int(m.group(2) or lo), []))
            continue
        current = tok
        ports.setdefault(current, [])
    return ports


def _routes(value: str) -> list[str]:
    lines = [ln.strip() for ln in value.splitlines() if ln.strip()]
    if len(lines) > 1:
        return lines
    # Flattened (legacy): split before each route's destination.
    return [
        r.strip()
        for r in re.split(r"(?<!via)(?<!src)\s+(?=(?:default|\d+\.\d+\.\d+\.\d+(?:/\d+)?) (?:via|dev) )", value)
        if r.strip()
    ]


def format_netdiag(parts: list[tuple[str, str]]) -> str:
    """The ``(label, value)`` pairs as a sectioned, aligned report."""
    if not parts:
        return ""
    lines = ["Network diagnostics (captured because not every check passed)", ""]
    by: dict[str, str] = {}
    guests: dict[str, list[tuple[str, str]]] = {}
    for label, value in parts:
        value = (value or "").strip()
        m = _IP_RE.match(label)
        t = _TCP_RE.match(label)
        if m:
            guests.setdefault(m.group(2), []).append((m.group(1), value))
        elif t:
            guests.setdefault(t.group(1), []).append((f"tcp {t.group(2)}", value))
        else:
            by[label] = value

    def row(key: str, text: str) -> None:
        lines.append(f"  {key:<10} {text}")

    if "bridge" in by:
        v = by["bridge"]
        if _is_err(v):
            lines.append(f"Sandbox bridge: missing ({v[3:].strip() or 'error'})")
        else:
            tok = v.split()
            name, state = [*tok, "?", "?"][:2]
            lines.append(f"Sandbox bridge {name}: {state}")
        members: list[str] = []
        if "bridge ports" in by:
            members = [p for p in by["bridge ports"].split() if p not in ("none", "OK")]
            row("ports", ", ".join(members) or "none — no NIC attached")
        if "bridge vlans" in by and not _is_err(by["bridge vlans"]):
            vlans = _parse_vlans(by["bridge vlans"])
            shown = [p for p in vlans if p in members or p == name] if members else list(vlans)
            for i, port in enumerate(shown):
                row("vlans" if i == 0 else "", f"{port}: {_vid_list(vlans[port])}")
            if members and not shown:
                row("vlans", "bridge ports not in the captured VLAN table")
        lines.append("")

    if "vm nic" in by:
        nics = [n.strip() for n in re.split(r"\s+(?=net\d+: )", by["vm nic"]) if n.strip() and n.strip() != "OK"]
        lines.append("Restored VM")
        for nic in nics or ["no NIC configured"]:
            key, _, rest = nic.partition(": ")
            row(key if rest else "nic", ", ".join(rest.split(",")) if rest else key)
        lines.append("")

    if "sbx addr" in by or "routes" in by:
        lines.append("Helper sandbox leg")
        if "sbx addr" in by:
            v = by["sbx addr"]
            addrs = re.findall(r"inet (\S+)", v)
            row("addresses", ", ".join(addrs) if addrs else (v if _is_err(v) else "none"))
        if "routes" in by:
            for i, r in enumerate(_routes(by["routes"])):
                row("routes" if i == 0 else "", r)
        lines.append("")

    for ip, checks in guests.items():
        lines.append(f"Guest {ip}")
        for kind, v in checks:
            if kind == "ping":
                rtt = re.search(r"time=([\d.]+ ms)", v)
                row("ICMP", "no answer" if _is_err(v) else f"answers ({rtt.group(1)})" if rtt else "answers")
            elif kind == "neigh":
                m = re.search(r"lladdr (\S+)\s+(\S+)", v)
                if m:
                    row("ARP", f"{m.group(1)}  {m.group(2)}")
                else:
                    state = re.search(r"\b(INCOMPLETE|FAILED|STALE|DELAY|PROBE)\b", v)
                    row("ARP", state.group(1) + " — no MAC, guest not on this L2/VLAN" if state else "no entry")
            else:
                row(kind.replace("tcp", "TCP"), "no answer" if _is_err(v) else "open")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_helper_netdiag(parts: list[tuple[str, str]]) -> str:
    """Readable render of the helper container's OWN network state (its
    management leg), captured when the helper agent was unreachable. ``parts``
    are ``(label, raw output)`` for ``addr`` / ``route`` / ``gw-ping``. Kept
    separate from :func:`format_netdiag` (whose labels are the sandbox/guest
    path) so an unreachable-helper error reads as lines, not a one-line blob."""
    by = {label: (value or "").strip() for label, value in parts}
    lines = ["Helper network (management leg unreachable)"]

    def row(key: str, text: str) -> None:
        lines.append(f"  {key:<10} {text}")

    if "addr" in by:
        v = by["addr"]
        # `ip -4 -o addr show`: "2: sbx    inet 100.64.0.254/24 scope global …".
        addrs = [f"{iface} {ip}" for iface, ip in re.findall(r"\d+:\s+(\S+)\s+inet (\S+)", v)]
        row("addresses", ", ".join(addrs) if addrs else (v if _is_err(v) else "none"))
    if "route" in by:
        v = by["route"]
        if _is_err(v):
            row("routes", v)
        else:
            for i, r in enumerate(_routes(v)):
                row("routes" if i == 0 else "", r)
    if "gw-ping" in by:
        v = by["gw-ping"]
        rtt = re.search(r"time=([\d.]+ ms)", v)
        row("gateway", "no answer" if _is_err(v) else (f"answers ({rtt.group(1)})" if rtt else "answers"))
    return "\n".join(lines)


def parse_legacy(text: str) -> list[tuple[str, str]] | None:
    """``probe netdiag [a: x || b: y]`` → ``[("a", "x"), ("b", "y")]``; ``None``
    when ``text`` is not in that form."""
    text = text.strip()
    if not (text.startswith(LEGACY_PREFIX) and text.endswith("]")):
        return None
    pairs = []
    for chunk in text[len(LEGACY_PREFIX) : -1].split(" || "):
        label, sep, value = chunk.partition(": ")
        if not sep:
            return None
        pairs.append((label.strip(), value))
    return pairs


def format_run_log(text: str | None) -> str:
    """A run's stored log for display: legacy one-line netdiag re-rendered,
    anything else unchanged."""
    if not text:
        return ""
    pairs = parse_legacy(text)
    return format_netdiag(pairs) if pairs else text
