// Phase 4 ("Activity becomes Home") regression test:
//
//   1. fetchAndScore() chains the two existing runs in the FRONTEND only — the
//      fetch poll's `done` branch triggers exactly one score-batch POST, and
//      nothing is triggered when the fetch was cancelled or failed.
//   2. The retired #fit-breakdown page resolves as a redirect: the hash lands
//      on Activity with the breakdown modal open.
//
// Same jsdom-as-one-eval-unit style as test_foundry.js: the production scripts
// are eval'd unmodified as ONE unit so top-level function declarations become
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
     <button id="run-status-chip" style="display:none"><span id="run-status-chip-text"></span></button>
     <div id="now-panel" hidden></div>
     <nav><a class="tab" data-tab="dashboard"></a><a class="tab" data-tab="jobs"></a></nav>
     <h2 id="page-title"></h2>
     <section id="dashboard" class="tab-content"></section>
     <section id="jobs" class="tab-content"></section>
     <div id="source-checkboxes"><input type="checkbox" value="linkedin" checked></div>
     <button id="fetch-score-btn"></button>
     <button id="fetch-btn">Fetch</button>
     <button id="fetch-stop-btn" style="display:none"></button>
     <button id="fetch-finish-btn" style="display:none"></button>
     <button id="score-btn">Score</button>
     <button id="score-stop-btn" style="display:none"></button>
     <input type="checkbox" id="score-rescore-cb">
     <select id="score-limit-select"><option value="" selected></option></select>
     <div id="run-log">
       <div id="run-log-history"></div>
       <div id="run-log-events"></div>
       <div id="run-log-live"></div>
       <div id="run-log-foot"></div>
     </div>
     <div id="fit-modal" style="display:none">
       <div id="fit-breakdown-error"></div>
       <canvas id="fit-pie-canvas" width="10" height="10"></canvas>
       <div id="fit-pie-avg"></div><div id="fit-legend"></div>
       <div id="fit-stat-list"></div><div id="fit-status-bars"></div>
     </div>
   </body></html>`,
  { runScripts: "dangerously", pretendToBeVisual: true, url: "http://localhost:8888/", virtualConsole }
);
const { window } = dom;
const doc = window.document;

const SCRIPTS = ["pipeline-stages.js", "core.js", "dashboard.js", "job-actions.js", "jobs.js", "review.js", "jobs-actions.js"];
window.eval(SCRIPTS.map((f) => fs.readFileSync(path.join(JS_DIR, f), "utf8")).join("\n;\n"));

const checks = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- Stubs: no network, no side effects from the post-fetch hooks ----
let posts = [];
let fetchStatus = { active: true, phase: "fetching", sources_done: 0, sources_total: 1 };
window.toast = () => {};
window.loadJobs = () => {};
window.loadDashboard = () => {};
window.loadSources = () => Promise.resolve();
window.hideBanner = () => {};
window.checkAIStatus = () => {};
window.api = (url, opts) => {
  if (opts && opts.method === "POST") posts.push(url);
  if (url === "/api/jobs/fetch/status") return Promise.resolve(fetchStatus);
  if (url === "/api/jobs/score-batch/status") return Promise.resolve({ status: "idle", done: 0, total: 0 });
  if (url === "/api/operations/status") return Promise.resolve({});
  if (url === "/api/stats") return Promise.resolve({ applied_today: 0, pending_review: 0 });
  return Promise.resolve({});
};

const scorePosts = () => posts.filter((u) => u.startsWith("/api/jobs/score-batch")).length;

let chainLabelWhileRunning = "", chainDisabledWhileRunning = false, _chipTextDuringFetch = "";

// The fetch poll ticks every 1500ms; each scenario waits out one tick.
async function runChain(finalStatus) {
  posts = [];
  fetchStatus = { active: true, phase: "fetching", sources_done: 0, sources_total: 1 };
  doc.getElementById("fetch-btn").disabled = false;
  doc.getElementById("score-btn").disabled = false;
  await window.fetchAndScore();
  chainLabelWhileRunning = doc.getElementById("fetch-score-btn").textContent;
  chainDisabledWhileRunning = doc.getElementById("fetch-score-btn").disabled;
  await sleep(1700);
  _chipTextDuringFetch = doc.getElementById("run-status-chip-text").textContent;
  fetchStatus = finalStatus;
  await sleep(2200);
  await sleep(0);
}

(async () => {
  // ---- 1. Happy path: fetch done → exactly one score run ----
  await runChain({ active: false, phase: "done", jobs_found: 12, jobs_inserted: 5, detail: "" });
  checks.push(["chain starts the fetch", posts.includes("/api/jobs/fetch")]);
  checks.push(["fetch done triggers the score run exactly once", scorePosts() === 1]);
  checks.push(["chain state is visible on the primary button", /then scoring/i.test(chainLabelWhileRunning)]);
  checks.push(["primary button is disabled while the chain runs", chainDisabledWhileRunning === true]);
  checks.push([
    "chip announces the chain while fetching",
    _chipTextDuringFetch.includes("then scoring"),
  ]);
  checks.push([
    "chained score run is tracked in the Now registry",
    window.nowRunsForRender().some((r) => r.kind === "score"),
  ]);

  // A second fetch (not chained) must NOT trigger scoring again.
  posts = [];
  fetchStatus = { active: true, phase: "fetching", sources_done: 0, sources_total: 1 };
  doc.getElementById("fetch-btn").disabled = false;
  doc.getElementById("score-btn").disabled = false;
  await window.fetchNewJobs();
  fetchStatus = { active: false, phase: "done", jobs_found: 3, jobs_inserted: 1, detail: "" };
  await sleep(2200);
  checks.push(["a plain Fetch never triggers scoring", scorePosts() === 0]);

  // ---- 2. Cancelled fetch: no score run ----
  await runChain({ active: false, phase: "done", jobs_found: 0, jobs_inserted: 0, detail: "Cancelled by user" });
  checks.push(["cancelled fetch does not trigger scoring", scorePosts() === 0]);

  // ---- 3. Failed fetch: no score run ----
  await runChain({ active: false, phase: "error", detail: "boom" });
  checks.push(["failed fetch does not trigger scoring", scorePosts() === 0]);

  // ---- 4. #fit-breakdown is a redirect to the modal over Activity ----
  const modal = doc.getElementById("fit-modal");
  window.location.hash = "fit-breakdown";
  window.handleHash();
  checks.push(["#fit-breakdown activates the Activity section", doc.getElementById("dashboard").classList.contains("active")]);
  checks.push(["#fit-breakdown has no page section of its own", doc.getElementById("fit-breakdown") === null]);
  checks.push(["#fit-breakdown opens the breakdown modal", modal.style.display !== "none"]);
  checks.push(["sidebar shows Activity as the active tab", doc.querySelector('nav .tab[data-tab="dashboard"]').classList.contains("active")]);

  // Navigating away closes it (and the hash is left alone by the router).
  window.location.hash = "jobs";
  window.handleHash();
  checks.push(["navigating away closes the breakdown modal", modal.style.display === "none"]);
  checks.push(["router did not rewrite the hash", window.location.hash === "#jobs"]);

  // Direct opener (stat card / histogram title) works without a hash change.
  window.openFitBreakdown();
  checks.push(["openFitBreakdown() opens it from the stat card", modal.style.display !== "none"]);
  window.closeFitBreakdown();
  checks.push(["closeFitBreakdown() closes it", modal.style.display === "none"]);

  const fail = report(checks);
  if (fail) {
    console.error(`\ntest_home: ${fail} check(s) failed`);
    process.exit(1);
  }
  console.log("\ntest_home.js: all checks passed");
  process.exit(0);
})();
