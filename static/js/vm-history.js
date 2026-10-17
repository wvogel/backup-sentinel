// ── Click a VM row → 30-day history charts (size, duration, throughput, reuse), one bar per day ─
(function () {
  const table = document.getElementById("vm-table");
  if (!table) return;
  const slug = decodeURIComponent(location.pathname.split("/")[2] || "");
  const INTERACTIVE = "a, button, select, input, textarea, label, form";

  // Same rules as app/services/human_format.py: one decimal below 10, whole
  // numbers above, unit scaled below 1000, decimal comma in German.
  const UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"];
  const LOCALE = typeof BSENTINEL_LANG !== "undefined" && BSENTINEL_LANG === "en" ? "en-US" : "de-DE";
  const fmtBytes = (v) => {
    let unit = 0;
    while (unit < UNITS.length - 1 && v >= 999.5) { v /= 1024; unit++; }
    const digits = unit > 0 && v < 9.95 ? 1 : 0;
    const n = digits ? Math.round(v * 10) / 10 : Math.round(v);
    return n.toLocaleString(LOCALE, { maximumFractionDigits: digits }) + " " + UNITS[unit];
  };
  const fmtDuration = (s) =>
    s < 3600 ? Math.max(1, Math.round(s / 60)) + " min"
    : s >= 10 * 3600 - 30 ? Math.round(s / 3600) + " h"
    : Math.floor(Math.round(s / 60) / 60) + " h " + String(Math.round(s / 60) % 60).padStart(2, "0") + " min";
  const fmtRate = (v) => fmtBytes(v) + "/s";
  const fmtPct = (v) => v + " %";

  const CHARTS = [
    { key: "size_bytes", title: "cluster.history_size", fmt: fmtBytes },
    { key: "duration_seconds", title: "cluster.history_duration", fmt: fmtDuration },
    { key: "throughput_bps", title: "cluster.history_throughput", fmt: fmtRate },
    { key: "reused_pct", title: "cluster.history_reused", fmt: fmtPct, max: 100 },
  ];

  function chart(events, spec) {
    const values = events.map((e) => e[spec.key]).filter((v) => v !== null && v !== undefined);
    const box = document.createElement("div");
    box.className = "vm-history-chart";
    const head = document.createElement("div");
    head.className = "vm-history-head";
    const title = document.createElement("span");
    title.textContent = T(spec.title);
    const last = document.createElement("strong");
    last.textContent = values.length ? spec.fmt(values[values.length - 1]) : "–";
    head.append(title, last);
    const bars = document.createElement("div");
    bars.className = "vm-history-bars";
    const max = spec.max || Math.max(1, ...values);
    for (const e of events) {
      const v = e[spec.key];
      const bar = document.createElement("span");
      bar.className = "vm-history-bar is-" + (e.status === "ok" ? (e.full_backup ? "full" : "ok") : e.status);
      bar.style.height = v ? Math.max(4, Math.round((v / max) * 100)) + "%" : "4%";
      bar.dataset.tooltip = e.label + ": " + (v !== null && v !== undefined ? spec.fmt(v) : "–")
        + (e.backups > 1 ? " · " + T("cluster.history_runs").replace("{n}", e.backups) : "")
        + (e.full_backup ? " · " + T("spark.full") : "") + (e.status !== "ok" ? " · " + T("spark." + e.status) : "");
      bars.append(bar);
    }
    box.append(head, bars);
    return box;
  }

  // Only the kinds of bars that actually appear.
  function legend(events) {
    const kinds = new Set(events.map((e) => (e.status === "ok" ? (e.full_backup ? "full" : "ok") : e.status)));
    const box = document.createElement("div");
    box.className = "vm-history-legend";
    for (const kind of ["ok", "full", "deleted", "failed"]) {
      if (!kinds.has(kind)) continue;
      const item = document.createElement("span");
      const swatch = document.createElement("span");
      swatch.className = "vm-history-bar is-" + kind;
      item.append(swatch, T("cluster.history_legend_" + kind));
      box.append(item);
    }
    return box;
  }

  async function open(row) {
    const detail = document.createElement("tr");
    detail.className = "vm-detail-row";
    const cell = document.createElement("td");
    cell.colSpan = row.cells.length;
    const collapse = document.createElement("button");
    collapse.type = "button";
    collapse.className = "button-secondary vm-detail-close";
    collapse.textContent = T("cluster.collapse");
    collapse.addEventListener("click", () => close(row));
    cell.append(collapse);
    const settings = row.querySelector("template.vm-settings-template");
    if (settings) cell.append(settings.content.cloneNode(true));
    const box = document.createElement("div");
    box.className = "vm-history muted";
    box.textContent = T("cluster.history_loading");
    cell.append(box);
    detail.append(cell);
    row.after(detail);
    row.classList.add("is-expanded");
    row.setAttribute("aria-expanded", "true");
    try {
      const resp = await fetch(`/api/clusters/${encodeURIComponent(slug)}/vms/${row.dataset.vmId}/history`);
      const data = await resp.json();
      box.replaceChildren();
      box.classList.remove("muted");
      if (!data.events || !data.events.length) {
        box.textContent = T("cluster.history_empty");
        return;
      }
      for (const spec of CHARTS) box.append(chart(data.events, spec));
      box.append(legend(data.events));
    } catch {
      box.textContent = T("cluster.history_empty");
    }
  }

  function close(row) {
    const next = row.nextElementSibling;
    if (next && next.classList.contains("vm-detail-row")) next.remove();
    row.classList.remove("is-expanded");
    row.setAttribute("aria-expanded", "false");
  }

  table.tBodies[0].addEventListener("click", (event) => {
    if (event.target.closest(INTERACTIVE)) return;
    // A click on free space of the opened details closes them again (bars keep their tooltips).
    const detail = event.target.closest(".vm-detail-row");
    if (detail) {
      if (!event.target.closest(".vm-history-bars")) close(detail.previousElementSibling);
      return;
    }
    const row = event.target.closest("tr[data-vm-id]");
    if (!row) return;
    if (row.classList.contains("is-expanded")) close(row);
    else open(row);
  });

  // After a setting was saved (full page reload) reopen that VM's details.
  const reopenKey = "openVm_" + location.pathname;
  const reopen = sessionStorage.getItem(reopenKey);
  if (reopen) {
    sessionStorage.removeItem(reopenKey);
    const row = table.querySelector(`tr[data-vm-id="${CSS.escape(reopen)}"]`);
    if (row) open(row);
  }

  // A detail row follows its VM row when filters hide or show it.
  new MutationObserver(() => {
    for (const detail of table.querySelectorAll(".vm-detail-row")) {
      const row = detail.previousElementSibling;
      detail.classList.toggle("hidden", !row || row.classList.contains("hidden"));
    }
  }).observe(table.tBodies[0], { attributes: true, attributeFilter: ["class"], subtree: true });
})();
