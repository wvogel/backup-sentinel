// Runs in <head>, before the first paint: sets the theme, so a dark page does
// not flash light, and the phase of the brand animation.
(function () {
  var t = localStorage.getItem("bsentinel-theme") || "auto";
  var dark = t === "dark" || (t === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  var s = (Date.now() / 1000) % 60;
  document.documentElement.style.setProperty("--brand-glow-delay", "-" + s.toFixed(1) + "s");
})();
