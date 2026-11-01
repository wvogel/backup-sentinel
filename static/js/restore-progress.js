// Live-refresh the running-restore card on /restore. Polls unconditionally
// while on the page — so a restore that starts *after* the page loaded (the
// queue wasn't active yet) still shows up without a manual reload — and updates
// progress in place once its card is present.
(function () {
  if (location.pathname !== "/restore") return;
  const url = "/restore/progress.json";

  async function tick() {
    let data;
    try {
      const res = await fetch(url, { headers: { Accept: "application/json" } });
      if (!res.ok) return;
      data = await res.json();
    } catch {
      return; // transient; try again next tick
    }
    const running = data.running || [];
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
      }
      // The runs table lists the same run: update its status cell too, so the
      // row isn't stuck at its page-load percent while the ring climbs. The live
      // percent shows next to the running glyph; phase + progress go on the hover.
      const row = document.querySelector(`tr[data-run-id="${r.id}"]`);
      if (row) {
        const rpct = row.querySelector(".js-row-pct");
        if (rpct) rpct.textContent = r.percent + "%";
        const rstatus = row.querySelector(".js-row-status");
        if (rstatus) rstatus.dataset.tooltip = r.progress ? r.phase + " · " + r.progress : r.phase;
      }
    }
  }

  setInterval(tick, 3000);
})();
