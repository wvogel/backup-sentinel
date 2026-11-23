// Language and theme. The server hands over language and translations as a
// JSON data block (#bsentinel-i18n): the page carries no inline script, so the
// Content-Security-Policy can forbid them.
var BSENTINEL_I18N = JSON.parse(document.getElementById("bsentinel-i18n").textContent);
var BSENTINEL_LANG = BSENTINEL_I18N.lang;
var BSENTINEL_TRANSLATIONS = BSENTINEL_I18N.translations;
function T(key) { return BSENTINEL_TRANSLATIONS[key] || key; }

function toggleLanguage() {
  var langs = BSENTINEL_I18N.languages;
  var idx = langs.indexOf(BSENTINEL_LANG);
  var next = langs[(idx + 1) % langs.length];
  localStorage.setItem("bsentinel-lang", next);
  document.cookie = "bsentinel-lang=" + next + ";path=/;max-age=31536000;SameSite=Lax";
  location.reload();
}
(function syncLangCookie() {
  var saved = localStorage.getItem("bsentinel-lang");
  if (saved && saved !== BSENTINEL_LANG) {
    document.cookie = "bsentinel-lang=" + saved + ";path=/;max-age=31536000;SameSite=Lax";
    location.reload();
  }
  if (!saved) localStorage.setItem("bsentinel-lang", BSENTINEL_LANG);
})();
document.querySelectorAll(".js-lang-toggle").forEach(function (btn) {
  btn.addEventListener("click", toggleLanguage);
});

(function initThemeToggle() {
  var states = ["light", "dark", "auto"];
  var current = localStorage.getItem("bsentinel-theme") || "auto";
  var btn = document.getElementById("theme-toggle");
  applyTheme(current);
  if (btn) btn.addEventListener("click", function () {
    current = states[(states.indexOf(current) + 1) % states.length];
    localStorage.setItem("bsentinel-theme", current);
    applyTheme(current);
  });
  function applyTheme(t) {
    var html = document.documentElement;
    if (t === "light") html.setAttribute("data-theme", "light");
    else if (t === "dark") html.setAttribute("data-theme", "dark");
    else html.setAttribute("data-theme", matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    if (btn) btn.setAttribute("data-state", t);
  }
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
    if (current === "auto") applyTheme("auto");
  });
})();
