// Sources without setup: the fetch picker's defaults (core.js loadSources)
// and the wizard's Sources step (onboarding.js) — company suggestions shown
// ticked, merged into the watchlists on Finish, and keyed sources tucked away.
//
// Same jsdom style as test_setup_modes.js: real index.html, real settings.js /
// onboarding.js, and the source-picker block of core.js. api() is a routed
// stub; nothing touches the network.
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const ROOT = path.join(__dirname, "..");
const JS_DIR = path.join(ROOT, "js");
const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
const core = fs.readFileSync(path.join(JS_DIR, "core.js"), "utf8");
const pickerBlock = core.slice(core.indexOf("const SOURCE_LABELS = {"), core.indexOf("// Split a comma-separated list"));

function report(checks) {
  let fail = 0;
  for (const [name, ok] of checks) {
    console.log((ok ? "PASS" : "FAIL") + "  " + name);
    if (!ok) fail++;
  }
  return fail;
}

const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost:8888/", virtualConsole: new VirtualConsole() });
const { window } = dom;
const doc = window.document;

const DETAILS = [
  { name: "remoteok", kind: "feed", configured: true, default_on: true, note: "" },
  { name: "adzuna", kind: "keyed", configured: false, default_on: false, note: "" },
  { name: "greenhouse", kind: "watchlist", configured: false, default_on: true, note: "" },
  { name: "linkedin", kind: "feed", configured: true, default_on: true, note: "Slow and brittle" },
  { name: "usajobs", kind: "keyed", configured: true, default_on: true, note: "" },
  { name: "indeed", kind: "browser", configured: true, default_on: false, note: "Slow and brittle" },
];

let calls = [];
let sourcesReply = { sources: DETAILS.map(d => d.name), details: DETAILS };
let suggestReply = { suggestions: [], ai_error: null };
let cfg = {
  profile: { full_name: "Jane Doe" },
  search: { greenhouse_boards: ["example-company"], lever_companies: ["zapier"] },
  api_keys: {}, ai: { models: {} },
};
window.api = (url, opts = {}) => {
  calls.push({ url, method: opts.method || "GET", body: opts.body ? JSON.parse(opts.body) : null });
  if (url === "/api/sources" && sourcesReply instanceof Error) return Promise.reject(sourcesReply);
  const r = {
    "/api/onboarding/status": { needs_onboarding: true, on_device: { supported: false, available: false } },
    "/api/ai/providers": [],
    "/api/config": opts.method === "POST" ? { message: "ok" } : cfg,
    "/api/ai/triage/status": { state: "not_installed", size_bytes: 1, progress: 0 },
    "/api/ai/nli/status": { state: "off", size_bytes: 1, progress: 0 },
    "/api/sources": sourcesReply,
    "/api/sources/suggest-companies": suggestReply,
    "/api/sources/test-key": { ok: true, message: "Keys work." },
    "/api/onboarding/complete": {},
  }[url];
  return Promise.resolve(r === undefined ? {} : r);
};
window.toast = () => {};
window.esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
window.escapeHtml = window.esc;
window.splitCsvSmart = (v) => (v || "").split(",").map(s => s.trim()).filter(Boolean);
window.appConfirm = () => Promise.resolve(true);
window.tourStart = () => {};

window.eval(
  pickerBlock + "\n;\n"
  + ["settings.js", "onboarding.js"].map(f => fs.readFileSync(path.join(JS_DIR, f), "utf8")).join("\n;\n")
  + "\n;window._obState = _obState; window.SOURCE_LABELS = SOURCE_LABELS;"
);

const $ = id => doc.getElementById(id);
const tick = async (n = 8) => { for (let i = 0; i < n; i++) await new Promise(r => setTimeout(r, 0)); };
const box = name => doc.querySelector(`#source-checkboxes input[value="${name}"]`);
const posts = url => calls.filter(c => c.url === url && c.method === "POST");
const checks = [];

(async () => {
  await tick();

  // ---- 1. fetch picker defaults ----
  await window.loadSources();
  checks.push(["keyless feed ticked", box("remoteok").checked && !box("remoteok").disabled]);
  checks.push(["Indeed unticked by default (opt-in)", !box("indeed").checked && !box("indeed").disabled]);
  checks.push(["keyed source without keys is locked", box("adzuna").disabled && !box("adzuna").checked]);
  checks.push(["keyed source with keys is ticked", box("usajobs").checked && !box("usajobs").disabled]);
  checks.push(["LinkedIn and Indeed tagged slow", /slow/.test(box("linkedin").parentElement.textContent)
    && /slow/.test(box("indeed").parentElement.textContent)]);
  checks.push(["selection excludes Indeed and locked sources",
    JSON.stringify(window.getSelectedSources()) === '["remoteok","greenhouse","linkedin","usajobs"]']);
  window.toggleAllSources(true);
  checks.push(["All never ticks a locked source", !box("adzuna").checked && box("indeed").checked]);

  box("indeed").checked = true;
  box("remoteok").checked = false;
  // Keys got added in Settings meanwhile: adzuna unlocks and takes its default.
  sourcesReply = { sources: sourcesReply.sources, details: DETAILS.map(d => d.name === "adzuna" ? { ...d, configured: true, default_on: true } : d) };
  await window.loadSources();
  checks.push(["re-render keeps this session's ticks", box("indeed").checked && !box("remoteok").checked]);
  checks.push(["newly keyed source unlocks ticked", box("adzuna").checked && !box("adzuna").disabled]);

  $("source-checkboxes").innerHTML = "";
  sourcesReply = new Error("down");
  await window.loadSources();
  checks.push(["offline fallback: Indeed and keyed sources off", !box("indeed").checked && !box("adzuna").checked
    && !box("usajobs").checked && box("linkedin").checked]);
  sourcesReply = { sources: DETAILS.map(d => d.name), details: DETAILS };

  // ---- 2. wizard Sources step: no AI → no call, a pointer instead ----
  window.obOpen({ status: { needs_onboarding: true } });
  await tick();
  window._obState.aiLater = true;
  calls = [];
  window.obGoto(4);
  await tick();
  checks.push(["stepper label is Sources", doc.querySelector('.ob-step[data-step="4"] .ob-step-label').textContent === "Sources"]);
  checks.push(["ready sources list the keyless feeds", /RemoteOK/.test($("ob-ready-sources").textContent)
    && /LinkedIn/.test($("ob-ready-sources").textContent) && !/Adzuna/.test($("ob-ready-sources").textContent)]);
  checks.push(["no AI: suggester not called", posts("/api/sources/suggest-companies").length === 0]);
  checks.push(["no AI: says where to add companies", /Settings/.test($("ob-companies").textContent)]);
  checks.push(["keyed sources are collapsed", $("ob-adzuna-app-id").closest("details") && !$("ob-adzuna-app-id").closest("details").open]);
  checks.push(["LinkedIn slow note on the step", /slow and brittle/i.test($("ob-slow-note").textContent)]);

  // ---- 3. with AI: auto-suggest, shown ticked ----
  window.obOpen({ status: { needs_onboarding: true } });
  await tick();
  window._obState.ai = { mode: "cloud" };
  $("ob-keywords").value = "support engineer";
  $("ob-workday-password").value = "hunter2";
  suggestReply = { suggestions: [
    { name: "Acme", why: "Fits", origin: "ai", boards: [{ source: "greenhouse", slug: "acme", jobs: 4, config_key: "greenhouse_boards" }] },
    { name: "Beta", why: "", origin: "ai", boards: [{ source: "lever", slug: "beta", jobs: 2, config_key: "lever_companies" }] },
  ], ai_error: null };
  calls = [];
  window.obGoto(4);
  await tick();
  const req = posts("/api/sources/suggest-companies")[0];
  checks.push(["entering the step asks for suggestions once", posts("/api/sources/suggest-companies").length === 1]);
  checks.push(["request carries the unsaved keywords", req && JSON.stringify(req.body.search.keywords) === '["support engineer"]']);
  checks.push(["request never carries Workday credentials", req && !("workday_password" in req.body.profile) && !("workday_email" in req.body.profile)]);
  const ticks = [...doc.querySelectorAll("#ob-companies input[data-company-idx]")];
  checks.push(["suggestions render ticked", ticks.length === 2 && ticks.every(t => t.checked)]);

  window.obGoto(3); window.obGoto(4);
  await tick();
  checks.push(["Back then Next does not re-ask", posts("/api/sources/suggest-companies").length === 1]);

  // Untick Beta, then build the payload.
  ticks[1].checked = false;
  ticks[1].dispatchEvent(new window.Event("change"));
  let payload = window.obBuildPayload();
  checks.push(["ticked board merged into saved list (placeholder dropped)",
    JSON.stringify(payload.search.greenhouse_boards) === '["acme"]'
    && JSON.stringify(payload.search.greenhouse_companies) === '["acme"]']);
  checks.push(["unticked company's list untouched", !("lever_companies" in payload.search)]);

  // ---- 4. Finish writes it ----
  calls = [];
  window._obState.rerun = false;
  $("ob-name").value = "Jane Q"; $("ob-email").value = "j@q.io";
  await window.obFinish();
  await tick();
  const saved = posts("/api/config")[0];
  checks.push(["Finish saves the followed board", saved && JSON.stringify(saved.body.search.greenhouse_boards) === '["acme"]']);
  checks.push(["Finish carries USAJobs keys", saved && "usajobs_email" in saved.body.api_keys && "usajobs_api_key" in saved.body.api_keys]);

  // ---- 5. re-run diff shows the watchlist row and applies both greenhouse keys ----
  cfg = { ...cfg, profile: { full_name: "Jane Q", email: "j@q.io" } };
  window.obOpen({ status: { needs_onboarding: false, setup_mode: "cloud" } });
  await tick();
  window._obState.savedAI = { mode: "cloud" };
  calls = [];
  window.obGoto(4);
  await tick();
  checks.push(["re-run with saved AI still suggests", posts("/api/sources/suggest-companies").length === 1]);
  const rerunBeta = doc.querySelectorAll("#ob-companies input[data-company-idx]")[1];
  rerunBeta.checked = false;
  rerunBeta.dispatchEvent(new window.Event("change"));
  $("ob-name").value = "Jane Q"; $("ob-email").value = "j@q.io";
  await window.obShowDiff();
  const labels = [...doc.querySelectorAll("#ob-diff-list .ob-diff-field")].map(e => e.textContent);
  checks.push(["diff lists the Greenhouse watchlist", labels.includes("Greenhouse boards")]);
  checks.push(["diff skips untouched watchlists", !labels.includes("Lever companies")]);
  calls = [];
  await window.obApplyDiff();
  const applied = posts("/api/config")[0];
  checks.push(["apply writes greenhouse_boards and the legacy key",
    applied && JSON.stringify(applied.body.search.greenhouse_boards) === '["acme"]'
    && JSON.stringify(applied.body.search.greenhouse_companies) === '["acme"]']);

  // ---- 6. key test uses the wizard fields ----
  $("ob-adzuna-app-id").value = "id1"; $("ob-adzuna-app-key").value = "key1";
  calls = [];
  await window.obTestSourceKey("adzuna");
  const t = posts("/api/sources/test-key")[0];
  checks.push(["Test sends the wizard's Adzuna fields", t && t.body.source === "adzuna" && t.body.adzuna_app_id === "id1" && t.body.adzuna_app_key === "key1"]);
  checks.push(["Test result shown", /Keys work/.test($("ob-adzuna-test-status").textContent)]);

  const fail = report(checks);
  console.log(fail ? `\ntest_sources_setup.js: ${fail} check(s) failed` : "\ntest_sources_setup.js: all checks passed");
  process.exit(fail ? 1 : 0);
})();
