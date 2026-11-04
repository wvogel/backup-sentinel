// Live-refresh the running-restore card on /restore. Polls unconditionally
// while on the page — so a restore that starts *after* the page loaded (the
// queue wasn't active yet) still shows up without a manual reload — and updates
// progress in place once its card is present. The dashboard shows the same
// card for the one running restore and polls only while it is there.
(function () {
  const onDashboard = location.pathname === "/";
  if (location.pathname !== "/restore" && !(onDashboard && document.querySelector(".running-card"))) return;
  const url = "/restore/progress.json";

  // ── Live 5-minute graphs in the restore-host row ──────────────────────────
  // Each .rhost-graph box keeps a rolling per-series buffer and is redrawn as a
  // tone-in-tone line+area (line = the box border tone, fill = the panel tone)
  // from the raw numbers the poll carries; no SSH per poll (cached snapshot).
  // RAM/volumes plot the fill level over time, Disk/Network the throughput.
  const VW = 100, VH = 40, MAXN = 100; // ~5 min at the 3 s poll cadence
  const graphBoxes = [...document.querySelectorAll("[data-rhost] .rhost-graph")];
  const de = (s) => s.replace(".", ",");
  function fsize(b) {
    if (b == null) return "–";
    const u = ["B", "KB", "MB", "GB", "TB", "PB"];
    let i = 0;
    b = +b;
    while (i < u.length - 1 && b >= 1000) { b /= 1024; i++; }
    return de(b >= 100 || b === Math.round(b) ? String(Math.round(b)) : b.toFixed(1)) + " " + u[i];
  }
  function fbit(b) {
    if (b == null) return "–";
    const x = b * 8;
    if (x >= 1e9) { const g = x / 1e9; return de(g === Math.round(g) ? String(g) : g.toFixed(1)) + " Gb/s"; }
    return Math.round(x / 1e6) + " Mb/s";
  }
  const pct = (v, max) => (max > 0 ? Math.round((v / max) * 100) : 0);
  const peak = (a, b, floor) => Math.max(floor, ...a, ...(b || [floor]));
  function push(buf, v) { buf.push(Math.max(0, +v || 0)); if (buf.length > MAXN) buf.shift(); }
  function areaPath(s, max) {
    let d = `M 0 ${VH}`;
    s.forEach((v, i) => { d += ` L ${((VW * i) / Math.max(1, s.length - 1)).toFixed(2)} ${(VH * (1 - Math.min(v, max) / max)).toFixed(2)}`; });
    return d + ` L ${VW} ${VH} Z`;
  }
  function linePath(s, max) {
    let d = "";
    s.forEach((v, i) => { d += (i ? " L" : "M") + ` ${((VW * i) / Math.max(1, s.length - 1)).toFixed(2)} ${(VH * (1 - Math.min(v, max) / max)).toFixed(2)}`; });
    return d;
  }
  function draw(box, series, max, ovHTML) {
    const svg = box.querySelector(".rhost-surf");
    if (!svg || !(max > 0)) return;
    let h = "";
    series.forEach((data, i) => {
      if (!data.length) return;
      if (i === 0) h += `<path d="${areaPath(data, max)}" fill="var(--panel)"/>`;
      h += `<path d="${linePath(data, max)}" fill="none" stroke="var(--line)" stroke-width="${i === 0 ? 1.6 : 1.2}"${i === 0 ? "" : ' stroke-dasharray="3 2" opacity="0.7"'} stroke-linejoin="round" vector-effect="non-scaling-stroke"/>`;
    });
    svg.innerHTML = h;
    const el = box.querySelector(".rhost-ov");
    if (el) el.innerHTML = ovHTML;
  }
  // One poll's raw host numbers → append to each box's buffer(s) and redraw.
  function updateGraphs(res) {
    if (!res) return;
    for (const box of graphBoxes) {
      box._buf = box._buf || [[], []];
      const m = box.dataset.metric;
      if (m === "ram" && res.ram) {
        const { used, total } = res.ram;
        push(box._buf[0], used);
        draw(box, [box._buf[0]], total, `<span class="mut">belegt</span> <b>${fsize(used)}</b> <span class="mut">· ${pct(used, total)} %</span>`);
      } else if (m === "store") {
        const s = (res.stores || []).find((x) => x.name === box.dataset.name);
        if (!s) continue;
        push(box._buf[0], s.used);
        draw(box, [box._buf[0]], s.total, `<span class="mut">belegt</span> <b>${fsize(s.used)}</b> <span class="mut">· ${pct(s.used, s.total)} %</span>`);
      } else if (m === "disk" && res.io) {
        push(box._buf[0], res.io.disk_write);
        push(box._buf[1], res.io.disk_read);
        const max = res.io.disk_max || peak(box._buf[0], box._buf[1], 1);
        draw(box, [box._buf[0], box._buf[1]], max, `<span class="mut">S</span> <b>${fsize(res.io.disk_write)}/s</b> <span class="mut">· L ${fsize(res.io.disk_read)}/s · ~${pct(res.io.disk_write, max)} %</span>`);
      } else if (m === "net" && res.io) {
        push(box._buf[0], res.io.net_in);
        push(box._buf[1], res.io.net_out);
        const max = res.io.net_link || peak(box._buf[0], box._buf[1], 1);
        draw(box, [box._buf[0], box._buf[1]], max, `<span class="mut">↓</span> <b>${fbit(res.io.net_in)}</b> <span class="mut">· ↑ ${fbit(res.io.net_out)} · ~${pct(res.io.net_in, max)} %</span>`);
      }
    }
  }

  async function tick() {
    let data;
    try {
      const res = await fetch(url, { headers: { Accept: "application/json" } });
      if (!res.ok) return;
      data = await res.json();
    } catch {
      return; // transient; try again next tick
    }
    // Rendered before the first host fetch finished: reload once it has an answer.
    if (data.host && data.host !== "loading" && document.querySelector("[data-host-loading]")) {
      location.reload();
      return;
    }
    updateGraphs(data.res);
    // The dashboard card shows only the first running restore.
    const running = onDashboard ? (data.running || []).slice(0, 1) : data.running || [];
    const runningIds = running
      .map((r) => String(r.id))
      .sort()
      .join(",");
    const cards = [...document.querySelectorAll(".running-card")];
    const cardIds = cards
      .map((c) => c.dataset.runId)
      .sort()
      .join(",");
    // The set of running restores changed (one started or finished): reload so
    // the card list and the runs table reflect it, then update in place after.
    if (runningIds !== cardIds) {
      location.reload();
      return;
    }
    for (const r of running) {
      const card = document.querySelector(`.running-card[data-run-id="${r.id}"]`);
      if (card) {
        const ring = card.querySelector(".running-ring");
        if (ring) ring.style.setProperty("--pct", r.percent);
        const pct = card.querySelector(".js-run-pct");
        if (pct) pct.textContent = r.percent + "%";
        const phase = card.querySelector(".js-run-phase");
        if (phase) phase.textContent = r.phase;
        const prog = card.querySelector(".js-run-progress");
        if (prog) prog.textContent = r.progress;
        // Live: the detected/DHCP IP and the checks already green this pass.
        const ip = card.querySelector(".js-run-ip");
        if (ip) {
          if (r.detected_ip) {
            ip.textContent = (ip.dataset.ipLabel || "IP") + " " + r.detected_ip;
            ip.hidden = false;
          } else {
            ip.hidden = true;
          }
        }
        const checks = card.querySelector(".js-run-checks");
        if (checks) {
          checks.replaceChildren();
          for (const c of r.checks_green || []) {
            const chip = document.createElement("span");
            chip.className = "check-chip";
            chip.textContent = "✓ " + c;
            checks.appendChild(chip);
          }
        }
        // Live ETA (only while restoring; "" otherwise → hide). Shown permanently
        // by the ring while present.
        const eta = card.querySelector(".js-run-eta");
        if (eta) {
          if (r.eta) {
            eta.textContent = (eta.dataset.etaLabel || "ETA") + " " + r.eta;
            eta.hidden = false;
          } else {
            eta.textContent = "";
            eta.hidden = true;
          }
        }
      }
      // The runs table lists the same run: update its status cell too, so the
      // row isn't stuck at its page-load percent while the ring climbs. The live
      // percent shows next to the running glyph; phase + progress go on the hover.
      const row = document.querySelector(`tr[data-run-id="${r.id}"]`);
      if (row) {
        const rpct = row.querySelector(".js-row-pct");
        if (rpct) rpct.textContent = r.percent + "%";
        const rstatus = row.querySelector(".js-row-status");
        if (rstatus) {
          let tip = r.phase;
          if (r.progress) tip += " · " + r.progress;
          if (r.eta) tip += " · ETA " + r.eta; // on hover, during restoring
          rstatus.dataset.tooltip = tip;
        }
      }
    }
  }

  setInterval(tick, 3000);
  // Waiting for the host: ask sooner than the regular cadence.
  if (document.querySelector("[data-host-loading]")) setTimeout(tick, 1000);
})();
