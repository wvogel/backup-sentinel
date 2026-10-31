// Live-refresh the running-restore card(s) on /restore: a restore — and its
// slow post-copy volume rescan / cache flush — can run for minutes, so poll the
// server for progress instead of making the operator reload the page.
(function () {
  const box = document.querySelector(".running-restores[data-poll]");
  if (!box) return;
  const url = box.dataset.poll;

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
    const cards = [...box.querySelectorAll(".running-card")];
    // The set of running restores changed (one finished, or a new one started):
    // reload so the runs table and card list reflect it.
    if (running.length !== cards.length) {
      location.reload();
      return;
    }
    for (const r of running) {
      const card = box.querySelector(`.running-card[data-run-id="${r.id}"]`);
      if (!card) {
        location.reload();
        return;
      }
      const ring = card.querySelector(".running-ring");
      if (ring) ring.style.setProperty("--pct", r.percent);
      const pct = card.querySelector(".js-run-pct");
      if (pct) pct.textContent = r.percent + "%";
      const phase = card.querySelector(".js-run-phase");
      if (phase) phase.textContent = r.phase;
      const prog = card.querySelector(".js-run-progress");
      if (prog) prog.textContent = r.progress;
    }
  }

  setInterval(tick, 3000);
})();
