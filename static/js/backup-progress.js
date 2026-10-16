// ── Running backups: live status bar + row progress ─────────────────────────
(function () {
  const table = document.getElementById("vm-table");
  const banner = document.querySelector(".js-running-banner");
  if (!table || !banner) return;

  const slug = decodeURIComponent(location.pathname.split("/")[2] || "");
  const POLL_RUNNING_MS = 30000;
  const POLL_IDLE_MS = 120000;

  function countLabel(count) {
    const key = count === 1 ? "cluster.running_one" : "cluster.running_other";
    return T(key).replace("{count}", count);
  }

  // A <tr> background is painted per cell in some browsers; give every cell
  // the full row width and its own offset so the fill reads as one bar.
  function layoutRows() {
    for (const tr of table.querySelectorAll("tr.is-running")) {
      const first = tr.cells[0];
      if (!first) continue;
      const width = tr.getBoundingClientRect().width;
      const left = first.getBoundingClientRect().left;
      for (const td of tr.cells) {
        td.style.setProperty("--row-w", width + "px");
        td.style.setProperty("--cell-x", left - td.getBoundingClientRect().left + "px");
      }
    }
  }

  // One tile per running backup: progress ring, name, detail lines.
  function renderBanner() {
    const rows = table.querySelectorAll("tbody tr.is-running");
    const list = banner.querySelector(".js-running-list");
    banner.querySelector(".js-running-count").textContent = countLabel(rows.length);
    list.replaceChildren();
    for (const tr of rows) {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "running-item";
      btn.dataset.vmid = tr.dataset.vmid;
      btn.title = T("cluster.running_jump");

      const pct = tr.style.getPropertyValue("--pct");
      const ring = document.createElement("span");
      ring.className = "topo-ring running-ring" + (pct === "" ? " is-indeterminate" : "");
      ring.style.setProperty("--p", pct || 0);
      const label = document.createElement("span");
      label.textContent = pct === "" ? "" : Math.round(Number(pct)) + "%";
      ring.append(label);

      const body = document.createElement("span");
      body.className = "running-item-body";
      const name = document.createElement("span");
      name.className = "running-item-name";
      name.textContent = tr.dataset.vmName + " ";
      const id = document.createElement("span");
      id.className = "muted";
      id.textContent = tr.dataset.vmid;
      name.append(id);
      body.append(name);
      const details = (tr.dataset.runningDetails || T("cluster.backup_running")).split("\n");
      for (const text of details) {
        const line = document.createElement("span");
        line.className = "running-item-meta";
        line.textContent = text;
        body.append(line);
      }
      if (tr.dataset.runningWarning) {
        const warn = document.createElement("span");
        warn.className = "running-slow";
        warn.textContent = tr.dataset.runningWarning;
        body.append(warn);
      }
      btn.append(ring, body);
      li.append(btn);
      list.append(li);
    }
    banner.classList.toggle("hidden", rows.length === 0);
  }

  function applyProgress(progress) {
    for (const tr of table.querySelectorAll("tbody tr[data-vmid]")) {
      const info = progress[tr.dataset.vmid];
      if (info) {
        tr.classList.add("is-running");
        tr.dataset.live = "1";
        tr.dataset.runningLine = info.line;
        tr.dataset.runningDetails = (info.details || []).join("\n");
        tr.dataset.runningWarning = info.warning || "";
        if (info.percent === null || info.percent === undefined) tr.style.removeProperty("--pct");
        else tr.style.setProperty("--pct", info.percent);
        tr.title = [info.line, info.warning, info.title].filter(Boolean).join("\n");
      } else if (tr.dataset.live === "1") {
        // Finished since the last poll; the new backup date arrives with the next sync.
        tr.classList.remove("is-running");
        tr.style.removeProperty("--pct");
        delete tr.dataset.live;
        delete tr.dataset.runningLine;
        delete tr.dataset.runningDetails;
        delete tr.dataset.runningWarning;
        tr.removeAttribute("title");
      }
    }
    renderBanner();
    layoutRows();
    document.dispatchEvent(new CustomEvent("backup-progress-updated"));
  }

  async function poll() {
    let running = table.querySelector("tr.is-running") !== null;
    try {
      const resp = await fetch(`/api/clusters/${encodeURIComponent(slug)}/sync-status?since=999999`);
      if (resp.ok) {
        const data = await resp.json();
        applyProgress(data.backup_progress || {});
        running = table.querySelector("tr.is-running") !== null;
      }
    } catch { /* keep the last state */ }
    setTimeout(poll, running ? POLL_RUNNING_MS : POLL_IDLE_MS);
  }

  banner.addEventListener("click", (event) => {
    const item = event.target.closest(".running-item");
    if (!item) return;
    const tr = table.querySelector(`tbody tr[data-vmid="${CSS.escape(item.dataset.vmid)}"]`);
    if (!tr) return;
    if (tr.offsetParent === null) {
      // Hidden by the node filter — reset to "all" first.
      const all = document.querySelector('[data-node-filter=""]');
      if (all) all.click();
    }
    tr.scrollIntoView({ behavior: "smooth", block: "center" });
    tr.classList.remove("running-flash");
    void tr.offsetWidth;
    tr.classList.add("running-flash");
  });

  window.addEventListener("resize", layoutRows);
  window.addEventListener("load", layoutRows);
  new ResizeObserver(layoutRows).observe(table);
  layoutRows();
  // Rendered without live data (nothing cached yet): fetch it right away.
  const first = banner.dataset.pending ? 1500 : table.querySelector("tr.is-running") ? POLL_RUNNING_MS : POLL_IDLE_MS;
  setTimeout(poll, first);
})();
