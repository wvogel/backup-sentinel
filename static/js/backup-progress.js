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
      const name = document.createElement("span");
      name.className = "running-item-name";
      name.textContent = tr.dataset.vmName + " ";
      const id = document.createElement("span");
      id.className = "muted";
      id.textContent = "(" + tr.dataset.vmid + ")";
      name.append(id);
      const meta = document.createElement("span");
      meta.className = "running-item-meta";
      meta.textContent = tr.dataset.runningLine || T("cluster.backup_running");
      const bar = document.createElement("span");
      bar.className = "running-bar";
      const fill = document.createElement("span");
      fill.style.width = (tr.style.getPropertyValue("--pct") || 0) + "%";
      bar.append(fill);
      btn.append(name, meta, bar);
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
        if (info.percent === null || info.percent === undefined) tr.style.removeProperty("--pct");
        else tr.style.setProperty("--pct", info.percent);
        tr.title = info.title ? `${info.line}\n${info.title}` : info.line;
      } else if (tr.dataset.live === "1") {
        // Finished since the last poll; the new backup date arrives with the next sync.
        tr.classList.remove("is-running");
        tr.style.removeProperty("--pct");
        delete tr.dataset.live;
        delete tr.dataset.runningLine;
        tr.removeAttribute("title");
      }
    }
    renderBanner();
    layoutRows();
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

  // Rows rendered as running by the server: remember their banner line.
  for (const item of banner.querySelectorAll(".running-item")) {
    const tr = table.querySelector(`tbody tr[data-vmid="${CSS.escape(item.dataset.vmid)}"]`);
    const meta = item.querySelector(".running-item-meta");
    if (tr && meta) tr.dataset.runningLine = meta.textContent;
  }

  window.addEventListener("resize", layoutRows);
  window.addEventListener("load", layoutRows);
  new ResizeObserver(layoutRows).observe(table);
  layoutRows();
  setTimeout(poll, table.querySelector("tr.is-running") ? POLL_RUNNING_MS : POLL_IDLE_MS);
})();
