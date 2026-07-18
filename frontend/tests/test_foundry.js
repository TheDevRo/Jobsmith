// Phase 2 "Foundry" regression test: the run console renders its verb buttons,
// the live-log line renderer escapes hostile HTML, the fit histogram bins
// compute from a fixture payload, and the Now-rail run registry adds /
// completes / expires runs on schedule.
//
// Same jsdom-as-one-eval-unit style as test_ledger.js: the production scripts
// are eval'd unmodified as ONE unit so top-level function declarations become
// window globals (const/let stay lexical to the eval and are exercised only
// through their observable effects). runScripts:"dangerously" + a real origin
// are required because core.js touches localStorage on load.
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const JS_DIR = path.join(__dirname, "..", "js");
const INDEX_HTML = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");

function report(checks) {
  let fail = 0;
  for (const [name, ok] of checks) {
    console.log((ok ? "PASS" : "FAIL") + "  " + name);
    if (!ok) fail++;
  }
  return fail;
}

const virtualConsole = new VirtualConsole();

// Minimal DOM: the run-console log slots + the global Now rail. The scripts'
// top-level bodies are all declarations, so nothing else is needed at eval time.
const dom = new JSDOM(
  `<!DOCTYPE html><html><body>
     <div id="toast-container"></div>
     <div id="run-status-chip"></div><span id="run-status-chip-text"></span>
     <div id="run-log">
       <div id="run-log-history"></div>
       <div id="run-log-events"></div>
       <div id="run-log-live"></div>
       <div id="run-log-foot"></div>
     </div>
     <aside id="now-rail" hidden></aside>
     <div id="histogram-card"></div>
     <a id="histogram-title"></a>
     <div id="fit-histo"></div>
   </body></html>`,
  { runScripts: "dangerously", pretendToBeVisual: true, url: "http://localhost:8888/", virtualConsole }
);
const { window } = dom;

// One eval unit, order matches index.html (core → dashboard → jobs → review → jobs-actions).
const SCRIPTS = ["core.js", "dashboard.js", "jobs.js", "review.js", "jobs-actions.js"];
window.eval(
  SCRIPTS.map((f) => fs.readFileSync(path.join(JS_DIR, f), "utf8")).join("\n;\n")
);

// Keep the rail's "Today" line off the network.
window.api = () => Promise.resolve({});
window._lastStats = { applied_today: 2, pending_review: 5 };

const checks = [];
const TTL_MS = 10 * 60 * 1000; // mirrors NOW_RUN_TTL_MS in dashboard.js

// ---- 1. The run console renders the verb buttons (static markup in index.html) ----
checks.push(["Fetch verb button present", /id="fetch-btn"[^>]*>Fetch</.test(INDEX_HTML)]);
checks.push(["Score verb button present", /id="score-btn"[^>]*>Score</.test(INDEX_HTML)]);
checks.push(["Tailor verb button present", /id="tailor-btn"[^>]*>Tailor</.test(INDEX_HTML)]);
checks.push(["Estimate verb button present", /id="estimate-salaries-btn"[^>]*>Estimate</.test(INDEX_HTML)]);
checks.push(["More overflow menu present", INDEX_HTML.includes('id="more-caret"')]);
checks.push(["Fetch options popover present", INDEX_HTML.includes('id="run-popover-fetch"')]);
checks.push([
  "old .action-cards block is gone",
  !INDEX_HTML.includes('class="action-cards"'),
]);
checks.push([
  "source checkbox ids preserved for fetchNewJobs()",
  INDEX_HTML.includes('id="source-checkboxes"'),
]);
checks.push([
  "score rescore + limit ids preserved",
  INDEX_HTML.includes('id="score-rescore-cb"') && INDEX_HTML.includes('id="score-limit-select"'),
]);

// ---- 2. runLogLineHtml() escapes HTML in both action and details ----
const hostile = window.runLogLineHtml({
  time: Date.now(),
  action: "<b>fetch</b>",
  details: '<img src=x onerror=alert(1)>',
});
window.document.getElementById("run-log-live").innerHTML = hostile;
const liveEl = window.document.getElementById("run-log-live");
checks.push(["log line injects no <img> element", liveEl.querySelectorAll("img").length === 0]);
checks.push(["log line injects no <b> element", liveEl.querySelectorAll(".rl-msg b").length === 0]);
checks.push([
  "log line renders the details as escaped text",
  liveEl.querySelector(".rl-msg").textContent.includes("<img src=x onerror=alert(1)>"),
]);

// ---- 3. computeFitHistogram() bins the /api/fit-breakdown payload ----
const hist = window.computeFitHistogram({
  score_buckets: { unscored: 8, low: 5, mid: 20, high: 11 },
  total_jobs: 44,
});
checks.push(["histogram has four bins", hist.bins.length === 4]);
checks.push(["histogram total sums the buckets", hist.total === 44]);
checks.push(["histogram max is the mid bin (20)", hist.max === 20]);
checks.push([
  "bins are ordered unscored → low → mid → high",
  hist.bins.map((b) => b.key).join(",") === "unscored,low,mid,high",
]);
checks.push(["mid bin count is 20", hist.bins[2].count === 20]);
checks.push(["only the mid bin is flagged isMax", hist.bins.filter((b) => b.isMax).length === 1 && hist.bins[2].isMax]);
checks.push(["low bin pct is relative to the max (25)", hist.bins[1].pct === 25]);
checks.push(["unscored bin uses the muted (non-heat) color", hist.bins[0].color === "var(--text-muted)"]);
const emptyHist = window.computeFitHistogram({});
checks.push(["empty payload → total 0, no isMax", emptyHist.total === 0 && emptyHist.bins.every((b) => !b.isMax)]);

// ---- 4. Now-rail run registry: add → complete → expire ----
const rail = window.document.getElementById("now-rail");

// Add: an active run shows on the rail.
window.trackRun("fetch", { status: "active", pct: 10, progressText: "1/10" });
let runs = window.nowRunsForRender();
checks.push(["active run is tracked", runs.length === 1 && runs[0].kind === "fetch" && runs[0].status === "active"]);
checks.push(["rail is shown while a run is active", rail.hidden === false]);
checks.push(["active run renders a live log line with a bar", window.document.querySelectorAll("#run-log-live .rl-bar").length === 1]);

// Complete: a finished run stays visible (within TTL) and keeps its result.
const t0 = Date.now();
window.trackRun("fetch", { status: "done", pct: 100, result: "38 new jobs" });
runs = window.nowRunsForRender();
checks.push(["completed run still shown within TTL", runs.length === 1 && runs[0].status === "done"]);
checks.push(["completed run keeps its result summary", runs[0].result === "38 new jobs"]);
checks.push(["completed run has no live log line", window.document.querySelectorAll("#run-log-live .rl-line").length === 0]);
checks.push([
  "completing emits a run event line",
  window.document.querySelectorAll("#run-log-events .rl-line").length >= 1,
]);

// Expire: past the TTL the finished run drops off (both the read model and prune).
checks.push([
  "finished run is excluded once past the TTL",
  window.nowRunsForRender(t0 + TTL_MS + 1000).length === 0,
]);
window.pruneRuns(t0 + TTL_MS + 1000);
checks.push(["pruneRuns() drops the expired run", window.nowRunsForRender().length === 0]);
window.renderNowRail();
checks.push(["rail hides itself once no runs remain", rail.hidden === true]);

const fail = report(checks);
if (fail) {
  console.error(`\ntest_foundry: ${fail} check(s) failed`);
  process.exit(1);
}
console.log("\ntest_foundry.js: all checks passed");
process.exit(0);
