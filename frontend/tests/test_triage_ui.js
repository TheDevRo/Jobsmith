// Quick match: the Local match model option in the Scoring Tier picker shows the Quick match block, picking it
// POSTs install (loading the page only reads the status), the status line / Retry / Delete follow the backend,
// and the job detail names the engine that scored it. Same jsdom style as test_nli_beta.js.
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
window.api = (url, opts = {}) => { calls.push([url, opts.method || "GET"]); return Promise.resolve(next); };
window.toast = () => {};
window.escapeHtml = (s) => String(s == null ? "" : s);
window.eval(fs.readFileSync(path.join(ROOT, "js", "settings.js"), "utf8"));
window.eval(fs.readFileSync(path.join(ROOT, "js", "jobs.js"), "utf8"));

const sel = doc.getElementById("cfg-scoring-tier");
const block = doc.getElementById("ai-triage-block");
const line = doc.getElementById("ai-triage-status");
const shown = el => el.style.display !== "none";
const checks = [];
const base = { size_bytes: 134041736, progress: 0, error: null };

(async () => {
  checks.push(["option is in the scoring tier picker", [...sel.options].some(o => o.value === "local-match-model")]);
  sel.value = "strong";
  window.scoringTierChanged({ install: false });
  checks.push(["other tiers hide the block", !shown(block)]);

  next = { ...base, state: "not_installed" };
  sel.value = "local-match-model";
  window.scoringTierChanged({ install: false });
  await new Promise(r => setTimeout(r, 0));
  checks.push(["page load only reads the status", calls.some(c => c[0] === "/api/ai/triage/status") && !calls.some(c => c[1] === "POST")]);
  checks.push(["block shown for the Local match model", shown(block)]);

  next = { ...base, state: "downloading", progress: 0.5 };
  window.scoringTierChanged();
  await new Promise(r => setTimeout(r, 0));
  checks.push(["picking it POSTs install", calls.some(c => c[0] === "/api/ai/triage/install" && c[1] === "POST")]);
  checks.push(["downloading shows the percentage", /Downloading 50%/.test(line.textContent)]);
  checks.push(["size comes from the backend", doc.getElementById("ai-triage-size").textContent === "134 MB"]);

  window.renderTriageStatus({ ...base, state: "error", error: "checksum mismatch" });
  checks.push(["error + Retry", /checksum mismatch/.test(line.textContent) && shown(doc.getElementById("ai-triage-retry"))]);
  window.renderTriageStatus({ ...base, state: "ready", progress: 1 });
  checks.push(["ready + Delete", /Ready/.test(line.textContent) && shown(doc.getElementById("ai-triage-delete"))]);

  const src = r => window.scoreSourceLine(r);
  checks.push(["Quick match label", /Scored by Quick match · Good fit · 0.04 s/.test(src({ scored_by: "triage", bucket: "Good", score_seconds: 0.04 }))]);
  checks.push(["preview-only label", /Scored by Quick match · preview only · Poor fit/.test(src({ scored_by: "triage", bucket: "Poor", preview: true }))]);
  checks.push(["NLI label", /Scored by Local match model · 8.2 s/.test(src({ scored_by: "local_model", score_seconds: 8.2 }))]);
  checks.push(["endpoint label", /Scored by qwen3/.test(src({ scored_by: "endpoint:qwen3" }))]);
  checks.push(["old scores: no label", src({ matched_skills: [] }) === "" && src(null) === ""]);

  let fail = 0;
  for (const [name, ok] of checks) { console.log((ok ? "PASS" : "FAIL") + "  " + name); if (!ok) fail++; }
  if (fail) { console.error(`\ntest_triage_ui.js: ${fail} check(s) failed`); process.exit(1); }
  console.log("\ntest_triage_ui.js: all checks passed");
  process.exit(0);
})();
