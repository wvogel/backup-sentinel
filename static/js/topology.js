// Draws the lines of the cluster topology (templates/_topology.html):
// PVE → datastore (solid) and replication (dashed, coloured by last run).
// Lines only run through the gaps between columns: neighbours are joined
// directly, lines skipping a column use a lane above the boxes, lines back
// to an earlier column (mutual backup) a lane below them.
(function () {
  "use strict";
  var SVG_NS = "http://www.w3.org/2000/svg";
  var GAP = 18; // half the column gap
  var RADIUS = 8;

  // Polyline with rounded corners.
  function rounded(points) {
    var d = "M" + points[0][0] + "," + points[0][1];
    for (var i = 1; i < points.length - 1; i++) {
      var p = points[i - 1], c = points[i], n = points[i + 1];
      var r = Math.min(RADIUS, Math.hypot(c[0] - p[0], c[1] - p[1]) / 2, Math.hypot(n[0] - c[0], n[1] - c[1]) / 2);
      var a = [c[0] - Math.sign(c[0] - p[0]) * r, c[1] - Math.sign(c[1] - p[1]) * r];
      var b = [c[0] + Math.sign(n[0] - c[0]) * r, c[1] + Math.sign(n[1] - c[1]) * r];
      d += " L" + a[0] + "," + a[1] + " Q" + c[0] + "," + c[1] + " " + b[0] + "," + b[1];
    }
    var last = points[points.length - 1];
    return d + " L" + last[0] + "," + last[1];
  }

  function draw(root) {
    var svg = root.querySelector(".topology-lines");
    var data = root.querySelector(".topology-edges");
    if (!svg || !data) return;
    var edges = JSON.parse(data.textContent || "[]");
    var outer = root.getBoundingClientRect();
    var ox = outer.left + root.clientLeft - root.scrollLeft;
    var oy = outer.top + root.clientTop;
    var height = root.scrollHeight;
    var stacked = getComputedStyle(root).flexDirection === "column";
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    function anchor(id) {
      var el = root.querySelector('[data-node="' + id + '"]');
      if (!el) return null;
      var box = (el.closest(".topo-server") || el).getBoundingClientRect();
      var ring = el.querySelector(".topo-ring") || el;
      var r = ring.getBoundingClientRect();
      return {
        left: box.left - ox, right: box.right - ox, top: box.top - oy, bottom: box.bottom - oy,
        cx: box.left + box.width / 2 - ox, y: r.top + r.height / 2 - oy,
      };
    }

    var boxes = Array.prototype.map.call(root.querySelectorAll(".topo-server, .topo-cluster"), function (el) {
      var r = el.getBoundingClientRect();
      return { top: r.top - oy, bottom: r.bottom - oy };
    });
    // Stacked: is another box between the two, so a direct line would cross it?
    function blocked(a, b) {
      return boxes.some(function (box) { return box.top > a.bottom + 1 && box.bottom < b.top - 1; });
    }

    var topLane = 0, bottomLane = 0, sideLane = 0;
    edges.forEach(function (edge) {
      var a = anchor(edge.from), b = anchor(edge.to);
      if (!a || !b) return;
      var d;
      if (stacked) {
        if (!edge.back && b.top > a.bottom && !blocked(a, b)) {
          var my = (a.bottom + b.top) / 2;
          d = "M" + a.cx + "," + a.bottom + " C" + a.cx + "," + my + " " + b.cx + "," + my + " " + b.cx + "," + b.top;
        } else {
          var lx = 6 + 4 * sideLane++;
          d = rounded([[a.left, a.y], [lx, a.y], [lx, b.y], [b.left, b.y]]);
        }
      } else if (edge.back) {
        var low = height - 10 - 4 * bottomLane++;
        d = rounded([[a.right, a.y], [a.right + GAP, a.y], [a.right + GAP, low], [b.left - GAP, low], [b.left - GAP, b.y], [b.left, b.y]]);
      } else if (edge.span > 1) {
        var high = 10 + 4 * topLane++;
        d = rounded([[a.right, a.y], [a.right + GAP, a.y], [a.right + GAP, high], [b.left - GAP, high], [b.left - GAP, b.y], [b.left, b.y]]);
      } else {
        var mx = (a.right + b.left) / 2;
        d = "M" + a.right + "," + a.y + " C" + mx + "," + a.y + " " + mx + "," + b.y + " " + b.left + "," + b.y;
      }
      var path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", d);
      path.setAttribute("class", "topo-line topo-line-" + edge.kind + " topo-state-" + edge.state + (edge.minor ? " topo-minor" : ""));
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
