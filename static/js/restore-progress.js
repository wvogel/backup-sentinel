// Live-refresh the running-restore card on /restore. Polls unconditionally
// while on the page — so a restore that starts *after* the page loaded (the
// queue wasn't active yet) still shows up without a manual reload — and updates
// progress in place once its card is present. The dashboard shows the same
// card for the one running restore and polls only while it is there.
(function () {
  const onDashboard = location.pathname === "/";
  if (location.pathname !== "/restore" && !(onDashboard && document.querySelector(".running-card"))) return;
  const url = "/restore/progress.json";

  function setArc(sel, pct) {
    const arc = document.querySelector(sel);
    if (!arc) return;
    const c = parseFloat(arc.dataset.c) || 0;
    arc.setAttribute("stroke-dasharray", `${((pct || 0) / 100) * c} ${c}`);
  }
  function setText(sel, val) {
    const el = document.querySelector(sel);
    if (el) el.textContent = val;
  }
  // The ring hover tooltip reads data-tooltip fresh on each hover (common.js),
  // so refreshing the attribute is enough — no open tooltip to re-render.
  function setTip(sel, val) {
    const el = document.querySelector(sel);
    if (el && val) el.dataset.tooltip = val;
  }
  // Update the Disk-I/O and Network rings in place from the host snapshot the
  // poll carries — no reload, and no SSH per poll (the server reads a cached
  // snapshot). A snapshot without a prior sample (io.have false) leaves the
  // page-rendered "—" untouched.
  function updateIoRings(io) {
    if (!io || !io.have) return;
    setArc(".js-io-disk-read", io.disk_read_pct);
    setArc(".js-io-disk-write", io.disk_write_pct);
    setText(".js-io-disk-ctr", io.disk_ctr);
    setText(".js-io-disk-read-v", io.disk_read);
    setText(".js-io-disk-write-v", io.disk_write);
    setTip(".js-io-disk-ring", io.disk_tip);
    setArc(".js-io-net-in", io.net_in_pct);
    setArc(".js-io-net-out", io.net_out_pct);
    setText(".js-io-net-ctr", io.net_ctr);
    setText(".js-io-net-in-v", io.net_in);
    setText(".js-io-net-out-v", io.net_out);
    setTip(".js-io-net-ring", io.net_tip);
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
    updateIoRings(data.io);
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
