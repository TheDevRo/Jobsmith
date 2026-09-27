// Local AI model (beta) Settings controls: the switch mirrors the backend status,
// turning it on PUTs the setting (the backend starts the download), the status
// line reads Not installed / Downloading N% / Ready / Error, Retry shows only
// when something can be retried, and Delete only when a model is on disk.
//
// Same jsdom style as test_apple_intelligence.js: real index.html + settings.js,
// api() stubbed, no network.
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const ROOT = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");

const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost:8888/", virtualConsole: new VirtualConsole() });
const { window } = dom;
const doc = window.document;

let next = {};
const calls = [];
window.api = (url, opts = {}) => { calls.push([url, opts.method || "GET", opts.body || null]); return Promise.resolve(next); };
window.toast = () => {};
window.esc = (s) => String(s == null ? "" : s);
window.eval(fs.readFileSync(path.join(ROOT, "js", "settings.js"), "utf8"));

const cb = doc.getElementById("cfg-ai-nli-beta");
const line = doc.getElementById("ai-nli-status");
const retry = doc.getElementById("ai-nli-retry");
const del = doc.getElementById("ai-nli-delete");
const shown = el => el.style.display !== "none";
const checks = [];
const base = { size_bytes: 546242116, progress: 0, error: null };

(async () => {
  checks.push(["switch is in the AI tab", !!cb && !!cb.closest("#stab-integrations")]);
  checks.push(["help text is there", /Fills application forms from your profile without an LLM/.test(cb.closest("#ai-nli-block").textContent)]);

  window.renderNliStatus({ ...base, enabled: false, installed: false, state: "off" });
  checks.push(["off → unchecked, no status line, no buttons", !cb.checked && !shown(line) && !shown(retry) && !shown(del)]);
  checks.push(["download size comes from the backend", doc.getElementById("ai-nli-size").textContent === "546 MB"]);

  next = { ...base, enabled: true, installed: false, state: "downloading", progress: 0.43 };
  cb.checked = true;
  await window.saveNliBeta();
  const put = calls.find(c => c[0] === "/api/settings/nli-beta");
  checks.push(["turning it on PUTs enabled:true", put && put[1] === "PUT" && JSON.parse(put[2]).enabled === true]);
  checks.push(["downloading shows the percentage", /Downloading 43%/.test(line.textContent)]);
  checks.push(["no Delete while downloading", !shown(del)]);

  window.renderNliStatus({ ...base, enabled: true, installed: false, state: "error", error: "checksum mismatch" });
  checks.push(["error is surfaced with the reason", /Error: checksum mismatch/.test(line.textContent) && shown(retry)]);
  next = { ...base, enabled: true, installed: false, state: "downloading", progress: 0.1 };
  await window.installNliModel();
  checks.push(["Retry POSTs install", calls.some(c => c[0] === "/api/ai/nli/install" && c[1] === "POST")]);

  window.renderNliStatus({ ...base, enabled: true, installed: true, state: "ready", progress: 1 });
  checks.push(["ready → Ready, Delete offered, no Retry", /Ready/.test(line.textContent) && shown(del) && !shown(retry)]);

  window.renderNliStatus({ ...base, enabled: false, installed: true, state: "off" });
  checks.push(["off with a model on disk → Delete still offered", !cb.checked && shown(del)]);
  next = { ...base, enabled: false, installed: false, state: "off" };
  await window.deleteNliModel();
  checks.push(["Delete sends DELETE", calls.some(c => c[0] === "/api/ai/nli/model" && c[1] === "DELETE") && !shown(del)]);

  let fail = 0;
  for (const [name, ok] of checks) { console.log((ok ? "PASS" : "FAIL") + "  " + name); if (!ok) fail++; }
  if (fail) { console.error(`\ntest_nli_beta.js: ${fail} check(s) failed`); process.exit(1); }
  console.log("\ntest_nli_beta.js: all checks passed");
  process.exit(0);
})();
