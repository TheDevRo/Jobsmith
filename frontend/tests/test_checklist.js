// A3 test: the getting-started checklist must be cheap, honest, and
// self-retiring — it renders only what the caches say, fetches
// /api/onboarding/status at most once per page load, and auto-dismisses the
// first time all five rows pass (so configured users never really see it).
//
// Same jsdom-as-one-eval-unit style as test_first_run_hint.js: the production
// scripts are eval'd unmodified as ONE unit so top-level declarations become
// window globals.
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const JS_DIR = path.join(__dirname, "..", "js");

function report(checks) {
  let fail = 0;
  for (const [name, ok] of checks) {
    console.log((ok ? "PASS" : "FAIL") + "  " + name);
    if (!ok) fail++;
  }
  return fail;
}

const virtualConsole = new VirtualConsole();

const dom = new JSDOM(
  `<!DOCTYPE html><html><body>
     <div id="toast-container"></div>
     <div id="app-banners"></div>
     <section id="dashboard">
       <div class="section-card" id="getting-started-card" style="display:none">
         <div class="gs-head">
           <p class="eyebrow">Getting started</p>
           <button class="gs-dismiss" onclick="dismissChecklist()">Dismiss</button>
         </div>
         <div class="gs-rows" id="getting-started-rows"></div>
       </div>
     </section>
   </body></html>`,
  { runScripts: "dangerously", pretendToBeVisual: true, url: "http://localhost:8888/", virtualConsole }
);
const { window } = dom;
const doc = window.document;

const SCRIPTS = ["core.js", "dashboard.js"];
window.eval(
  SCRIPTS.map((f) => fs.readFileSync(path.join(JS_DIR, f), "utf8")).join("\n;\n")
);

const card = doc.getElementById("getting-started-card");
const rows = doc.getElementById("getting-started-rows");

// Only /api/onboarding/status may ever be requested from this path.
let apiCalls = [];
let onbPayload = { profile_ok: false, extension_paired: false };
window.api = (url) => {
  apiCalls.push(url);
  if (url === "/api/onboarding/status") return Promise.resolve(onbPayload);
  return Promise.reject(new Error("unexpected fetch: " + url));
};
window.toast = () => {};

const reset = (opts = {}) => {
  window.localStorage.clear();
  window._aiStatus = opts.ai;
  window._lastStats = opts.stats;
  window._onbStatus = opts.onb;
  apiCalls = [];
  rows.innerHTML = "";
  card.style.display = "none";
};

const shown = () => card.style.display !== "none";

(async () => {
  const checks = [];

  // ---- 1. Fresh install: nothing done, every row offers its action ----
  reset({ stats: { total_jobs: 0 } });
  onbPayload = { profile_ok: false, extension_paired: false };
  await window.renderGettingStarted();
  checks.push(["fresh install → card is shown", shown()]);
  checks.push(["renders all five rows", rows.querySelectorAll(".gs-row").length === 5]);
  checks.push(["no row is marked done", rows.querySelectorAll(".gs-row-done").length === 0]);
  const html = rows.innerHTML;
  for (const [name, needle] of [
    ["fetch", "stageFetch()"],
    ["wizard", "obOpen()"],
    ["jobs view", "location.hash='jobs'"],
    ["assist settings", "goAssistSettings()"],
  ]) {
    checks.push([`unchecked rows offer the ${name} action`, html.includes(needle)]);
  }
  // AI never probed → offer the probe rather than a pointless settings trip.
  checks.push(["AI never probed → offers a connection check",
    html.includes("checklistCheckAI()") && !html.includes("goAISettings()")]);
  checks.push(["only /api/onboarding/status is fetched",
    apiCalls.length === 1 && apiCalls[0] === "/api/onboarding/status"]);

  // AI probed and down → send them to the settings that fix it.
  window._aiStatus = { ok: false, base_url: "http://x" };
  await window.renderGettingStarted();
  checks.push(["AI known-down → offers AI settings", rows.innerHTML.includes("goAISettings()")]);
  checks.push(["status is cached, not refetched", apiCalls.length === 1]);

  // ---- 2. Partial progress marks only the rows that really passed ----
  reset({
    ai: { ok: true },
    stats: { total_jobs: 12, jobs_by_status: { shortlisted: 3 }, total_applied: 0 },
  });
  onbPayload = { profile_ok: true, extension_paired: false };
  await window.renderGettingStarted();
  checks.push(["four satisfied rows are checked", rows.querySelectorAll(".gs-row-done").length === 4]);
  checks.push(["the unmet row keeps its action", rows.innerHTML.includes("goAssistSettings()")]);
  checks.push(["done rows drop their action button",
    rows.querySelectorAll(".gs-action").length === 1]);
  checks.push(["card still visible while a row is unmet", shown()]);

  // Shortlisting counts jobs awaiting review too, not just the literal status.
  reset({ ai: { ok: true }, stats: { total_jobs: 5, pending_review: 2 } });
  onbPayload = { profile_ok: true, extension_paired: true };
  await window.renderGettingStarted();
  checks.push(["pending_review satisfies the shortlist row",
    rows.querySelectorAll(".gs-row-done").length === 5 || !shown()]);

  // Applications submitted before the flag existed still count as paired.
  reset({ ai: { ok: true }, stats: { total_jobs: 5, jobs_by_status: {}, total_applied: 4 } });
  onbPayload = { profile_ok: true, extension_paired: false };
  await window.renderGettingStarted();
  checks.push(["submitted applications imply a paired extension",
    !rows.innerHTML.includes("goAssistSettings()")]);

  // ---- 3. All green → auto-dismiss, silently and permanently ----
  reset({
    ai: { ok: true },
    stats: { total_jobs: 20, jobs_by_status: { shortlisted: 4 }, total_applied: 2 },
  });
  onbPayload = { profile_ok: true, extension_paired: true };
  await window.renderGettingStarted();
  checks.push(["all green → card never appears", !shown()]);
  checks.push(["all green → marked done in localStorage",
    window.localStorage.getItem("jobsmith_checklist_done") === "1"]);

  // Marked done → never renders again, and never fetches again either.
  apiCalls = [];
  window._onbStatus = undefined;
  window._lastStats = { total_jobs: 0 };
  await window.renderGettingStarted();
  checks.push(["dismissed checklist never renders again", !shown()]);
  checks.push(["dismissed checklist never fetches again", apiCalls.length === 0]);

  // ---- 4. Manual Dismiss link ----
  reset({ stats: { total_jobs: 0 } });
  onbPayload = { profile_ok: false, extension_paired: false };
  await window.renderGettingStarted();
  checks.push(["card shown before dismissal", shown()]);
  doc.querySelector(".gs-dismiss").click();
  checks.push(["Dismiss hides the card", !shown()]);
  checks.push(["Dismiss persists the flag",
    window.localStorage.getItem("jobsmith_checklist_done") === "1"]);

  // ---- 5. A failed status fetch means "say nothing", not "guess" ----
  reset({ ai: { ok: true }, stats: { total_jobs: 3 } });
  window.api = (url) => { apiCalls.push(url); return Promise.reject(new Error("offline")); };
  await window.renderGettingStarted();
  checks.push(["status fetch failure hides the card rather than guessing", !shown()]);
  checks.push(["status fetch failure renders no rows", rows.innerHTML === ""]);

  const fail = report(checks);
  if (fail) {
    console.error(`\ntest_checklist: ${fail} check(s) failed`);
    process.exit(1);
  }
  console.log("\ntest_checklist.js: all checks passed");
  process.exit(0);
})();
