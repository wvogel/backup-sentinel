// ── Table interaction ────────────────────────────────────────────────────────

// ── Sortierbare Tabellen ─────────────────────────────────────────────────────
for (const table of document.querySelectorAll(".js-sortable-table")) {
  const tbody = table.tBodies[0];
  if (!tbody) {
    continue;
  }

  const headers = Array.from(table.tHead?.rows[0]?.cells || []);
  for (const [index, header] of headers.entries()) {
    if (!header.classList.contains("sortable")) {
      continue;
    }

    const sortRows = () => {
      const currentDirection = header.dataset.direction === "asc" ? "asc" : (header.dataset.direction === "desc" ? "desc" : "none");
      const nextDirection = currentDirection === "asc" ? "desc" : "asc";
      const rows = Array.from(tbody.rows);
      const sortType = header.dataset.sortType || "text";

      for (const otherHeader of headers) {
        otherHeader.setAttribute("aria-sort", "none");
        if (otherHeader !== header) {
          otherHeader.dataset.direction = "none";
        }
      }

      rows.sort((leftRow, rightRow) => {
        const leftCell = leftRow.cells[index];
        const rightCell = rightRow.cells[index];
        const leftValue = (leftCell?.dataset.sortValue || leftCell?.textContent || "").trim();
        const rightValue = (rightCell?.dataset.sortValue || rightCell?.textContent || "").trim();

        if (sortType === "number") {
          return Number(leftValue) - Number(rightValue);
        }

        return leftValue.localeCompare(rightValue, undefined, {
          numeric: true,
          sensitivity: "base",
        });
      });

      if (nextDirection === "desc") {
        rows.reverse();
      }

      tbody.append(...rows);
      header.dataset.direction = nextDirection;
      header.setAttribute("aria-sort", nextDirection === "asc" ? "ascending" : "descending");
    };

    header.addEventListener("click", sortRows);
    header.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        sortRows();
      }
    });
  }
}

function applyRowVisibility(row) {
  const byNode = row.dataset.hiddenByNode === "true";
  const byStats = row.dataset.hiddenByStats === "true";
  row.classList.toggle("hidden", byNode || byStats);
}

for (const list of document.querySelectorAll(".js-node-filter-list")) {
  const container = list.closest(".table-card");
  const table = container?.querySelector(".js-node-filter-table");
  const tbody = table?.tBodies?.[0];
  const emptyState = container?.querySelector(".vm-filter-empty");
  if (!table || !tbody) {
    continue;
  }

  const rows = Array.from(tbody.rows);
  let activeNode = "";

  const applyNodeFilter = () => {
    for (const row of rows) {
      const passes = !activeNode || row.dataset.nodeName === activeNode;
      row.dataset.hiddenByNode = passes ? "false" : "true";
      applyRowVisibility(row);
    }

    for (const button of list.querySelectorAll(".node-filter-button")) {
      const buttonNode = button.dataset.nodeFilter || "";
      button.classList.toggle("is-active", buttonNode === activeNode || (!activeNode && buttonNode === ""));
    }

    if (emptyState) {
      const visibleRows = rows.filter((r) => !r.classList.contains("hidden")).length;
      emptyState.classList.toggle("hidden", visibleRows !== 0);
    }
  };

  for (const button of list.querySelectorAll(".node-filter-button")) {
    button.addEventListener("click", () => {
      const nextNode = button.dataset.nodeFilter || "";
      activeNode = activeNode === nextNode ? "" : nextNode;
      applyNodeFilter();
    });
  }
}

for (const tileGroup of document.querySelectorAll(".js-stat-filter-tiles")) {
  const targetId = tileGroup.dataset.filterTarget;
  const table = targetId ? document.querySelector(targetId) : tileGroup.closest("section")?.querySelector(".js-stat-filter-table");
  const tbody = table?.tBodies?.[0];
  if (!tbody) {
    continue;
  }

  const rows = Array.from(tbody.rows);
  const activeTags = new Set();

  const applyStatFilter = () => {
    for (const row of rows) {
      const tags = new Set((row.dataset.tags || "").split(" ").filter(Boolean));
      const passes = activeTags.size === 0 || [...activeTags].some((tag) => tags.has(tag));
      row.dataset.hiddenByStats = passes ? "false" : "true";
      applyRowVisibility(row);
    }

    for (const tile of tileGroup.querySelectorAll("[data-tile-filter]")) {
      tile.classList.toggle("is-filter-active", activeTags.has(tile.dataset.tileFilter));
    }
  };

  for (const tile of tileGroup.querySelectorAll("[data-tile-filter]")) {
    tile.style.cursor = "pointer";
    tile.addEventListener("click", () => {
      const tag = tile.dataset.tileFilter;
      if (activeTags.has(tag)) {
        activeTags.delete(tag);
      } else {
        activeTags.add(tag);
      }
      applyStatFilter();
    });
  }
}
