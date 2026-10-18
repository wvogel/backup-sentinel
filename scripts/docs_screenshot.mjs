// Documentation screenshots: headless Chrome (macOS path), dark mode, 2x, demo user.
// Usage: node scripts/docs_screenshot.mjs <url> <out.png> <width> <height> [en|de]
import { spawn } from "node:child_process";
import { writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
const [url, out, width, height, lang = "en"] = process.argv.slice(2);  // height: px, "full", or a CSS selector to end below
const chrome = spawn("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  ["--headless=new", "--remote-debugging-port=9333", "--no-first-run", "--hide-scrollbars",
   `--user-data-dir=${mkdtempSync(tmpdir() + "/bs-shot-")}`, "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let target;
for (let i = 0; i < 50 && !target; i++) {
  await sleep(200);
  try { target = (await (await fetch("http://127.0.0.1:9333/json")).json()).find((t) => t.type === "page"); } catch {}
}
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));
let id = 0; const pending = new Map();
ws.addEventListener("message", (e) => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } });
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const w = Number(width);
await send("Emulation.setDeviceMetricsOverride", { width: w, height: 900, deviceScaleFactor: 2, mobile: false });
await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }] });
await send("Network.enable");
await send("Network.setExtraHTTPHeaders", { headers: { "X-Forwarded-Email": "admin@example.com" } });
await send("Network.setCookie", { name: "bsentinel-lang", value: lang, url });
await send("Page.enable");
await send("Page.navigate", { url });
await sleep(4000);
const expr = height === "full"
  ? "Math.ceil(Math.max(...[...document.querySelectorAll('main > *, .page > *, footer')].map(e => e.getBoundingClientRect().bottom + scrollY)) + 16)"
  : /^\d+$/.test(height) ? height : `Math.ceil(document.querySelector(${JSON.stringify(height)}).getBoundingClientRect().bottom + scrollY + 20)`;
const metrics = await send("Runtime.evaluate", { expression: expr, returnByValue: true });
const h = Number(metrics.result.result.value);
await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 2, mobile: false });
await sleep(1500);
const shot = await send("Page.captureScreenshot", { format: "png", clip: { x: 0, y: 0, width: w, height: h, scale: 1 } });
writeFileSync(out, Buffer.from(shot.result.data, "base64"));
ws.close(); chrome.kill();
console.log("saved", out, w, h);
