// ── Garbage collection and verify running on a PBS ───────────────────────────
// The PBS symbols ([data-pbs-activity="<pbs id>"]) move and the datastore rings
// ([data-store-activity="<pbs id>:<store>"]) get a progress ring while a PBS
// collects garbage or verifies; the details are in the tooltip.
//
// The server asks a PBS only when this poll asks the server, so it runs only
// while someone looks at the page: every 30 s in a visible tab, not at all in
// a hidden one, and once right away when the tab comes back.
(function () {
  const POLL_MS = 30000;
  const KINDS = ["gc", "verify"];
  const CSS_VAR = { gc: "--pg", verify: "--pv" };
  let timer = null;
  let lastPoll = 0;

  function pbsIds() {
    const ids = new Set();
    document.querySelectorAll("[data-pbs-activity]").forEach((el) => ids.add(el.dataset.pbsActivity));
    document.querySelectorAll("[data-store-activity]").forEach((el) => {
      ids.add(el.dataset.storeActivity.split(":")[0]);
    });
    return [...ids].filter((id) => /^\d+$/.test(id));
  }

  // data-tooltip-base always shows, data-tooltip-busy only heads the details.
  function setTooltip(el, details) {
    const lines = [el.dataset.tooltipBase, details && el.dataset.tooltipBusy, details].filter(Boolean);
    if (lines.length) el.dataset.tooltip = lines.join("\n");
    else delete el.dataset.tooltip;
  }

  function apply(data) {
    document.querySelectorAll("[data-pbs-activity]").forEach((el) => {
      const info = data[el.dataset.pbsActivity];
      KINDS.forEach((kind) => el.classList.toggle("is-" + kind, Boolean(info && info.kinds.includes(kind))));
      setTooltip(el, info ? info.tooltip : "");
    });
    document.querySelectorAll("[data-store-activity]").forEach((el) => {
      const key = el.dataset.storeActivity;
      const cut = key.indexOf(":");
      const pbs = data[key.slice(0, cut)];
      const info = pbs && pbs.stores[key.slice(cut + 1)];
      const ring = el.querySelector(".topo-ring");
      if (ring) {
        KINDS.forEach((kind) => {
          const running = Boolean(info && kind in info.kinds);
          const percent = running ? info.kinds[kind] : null;
          ring.classList.toggle("has-" + kind, running);
          ring.classList.toggle(kind + "-indeterminate", running && percent === null);
          if (percent === null) ring.style.removeProperty(CSS_VAR[kind]);
          else ring.style.setProperty(CSS_VAR[kind], percent);
        });
      }
      setTooltip(el, info ? info.tooltip : "");
    });
    if (window.syncAnimations) window.syncAnimations();
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(poll, POLL_MS);
  }

  async function poll() {
    timer = null;
    if (document.hidden) return; // woken again by visibilitychange
    const ids = pbsIds();
    if (!ids.length) return;
    lastPoll = Date.now();
    try {
      const resp = await fetch("/api/pbs-activity?ids=" + ids.join(","));
      if (resp.ok) apply(await resp.json());
    } catch { /* keep the last state */ }
    schedule();
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearTimeout(timer);
      timer = null;
    } else if (timer === null) {
      const wait = Math.max(0, POLL_MS - (Date.now() - lastPoll));
      timer = setTimeout(poll, wait);
    }
  });

  document.addEventListener("DOMContentLoaded", () => {
    if (pbsIds().length) timer = setTimeout(poll, 1200);
  });
})();
