// Draws the lines of the cluster topology (templates/_topology.html):
// PVE → datastore (solid) and replication (dashed, coloured by last run).
(function () {
  "use strict";
  var SVG_NS = "http://www.w3.org/2000/svg";

  function draw(root) {
    var svg = root.querySelector(".topology-lines");
    var data = root.querySelector(".topology-edges");
    if (!svg || !data) return;
    var edges = JSON.parse(data.textContent || "[]");
    // Coordinates relative to the padding box the SVG is laid over.
    var outer = root.getBoundingClientRect();
    var box = { left: outer.left + root.clientLeft - root.scrollLeft, top: outer.top + root.clientTop };
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var stacked = getComputedStyle(root).flexDirection === "column";

    edges.forEach(function (edge) {
      var from = root.querySelector('[data-node="' + edge.from + '"]');
      var to = root.querySelector('[data-node="' + edge.to + '"]');
      if (!from || !to) return;
      var a = from.getBoundingClientRect();
      var b = to.getBoundingClientRect();
      var d;
      if (stacked) {
        var sx = a.left + a.width / 2 - box.left, sy = a.bottom - box.top;
        var tx = b.left + b.width / 2 - box.left, ty = b.top - box.top;
        if (edge.back) { sy = a.top - box.top; ty = b.bottom - box.top; }
        var my = (sy + ty) / 2;
        d = "M" + sx + "," + sy + " C" + sx + "," + my + " " + tx + "," + my + " " + tx + "," + ty;
      } else if (edge.back) {
        // Replication back to an earlier column: loop below both nodes.
        var bx = a.left + a.width / 2 - box.left, by = a.bottom - box.top;
        var ex = b.left + b.width / 2 - box.left, ey = b.bottom - box.top;
        var low = Math.max(by, ey) + 18;
        d = "M" + bx + "," + by + " C" + bx + "," + low + " " + ex + "," + low + " " + ex + "," + ey;
      } else {
        var x1 = a.right - box.left, y1 = a.top + Math.min(a.height / 2, 34) - box.top;
        var x2 = b.left - box.left, y2 = b.top + Math.min(b.height / 2, 34) - box.top;
        var mx = (x1 + x2) / 2;
        d = "M" + x1 + "," + y1 + " C" + mx + "," + y1 + " " + mx + "," + y2 + " " + x2 + "," + y2;
      }
      var path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", d);
      path.setAttribute("class", "topo-line topo-line-" + edge.kind + " topo-state-" + edge.state);
      svg.appendChild(path);
    });
  }

  function drawAll() {
    document.querySelectorAll("[data-topology]").forEach(draw);
  }

  document.addEventListener("DOMContentLoaded", function () {
    drawAll();
    if (window.ResizeObserver) {
      document.querySelectorAll("[data-topology]").forEach(function (root) {
        new ResizeObserver(function () { draw(root); }).observe(root);
      });
    } else {
      window.addEventListener("resize", drawAll);
    }
  });
})();
