// B2 regression test: firstRunHint() must stay SILENT for configured users
// (whose empty-state copy is byte-identical to before) and must only speak when
// the caches it reads positively say something is wrong. It must never fetch.
//
// Same jsdom-as-one-eval-unit style as test_deck.js: the production scripts are
// eval'd unmodified as ONE unit so top-level function declarations become window
// globals, and the helper is exercised purely through its return value plus the
// render paths that splice it in.
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
     <div id="pipeline-funnel"></div>
     <section id="jobs">
       <select id="filter-status"><option value="" selected></option><option value="deleted">deleted</option></select>
       <div id="jobs-list"></div>
       <div id="jobs-pagination"></div>
       <div id="inbox-stage" class="inbox-stage"></div>
     </section>
     <section id="review"><div id="shortlisted-list"></div><div id="review-list"></div></section>
     <div id="activity-feed"></div>
   </body></html>`,
  { runScripts: "dangerously", pretendToBeVisual: true, url: "http://localhost:8888/", virtualConsole }
);
const { window } = dom;
const doc = window.document;

const SCRIPTS = ["pipeline-stages.js", "core.js", "dashboard.js", "job-actions.js", "jobs.js", "review.js", "jobs-actions.js", "deck.js"];
window.eval(
  SCRIPTS.map((f) => fs.readFileSync(path.join(JS_DIR, f), "utf8")).join("\n;\n")
);

const checks = [];

// The helper must never touch the network; any api() call from it is a failure.
let apiCalls = 0;
window.api = (url) => { apiCalls++; return Promise.resolve({ jobs: [], total: 0 }); };
window.loadJobs = () => Promise.resolve();
window.toast = () => {};
window.clearDetailPane = () => {};
window.updateSelectedCount = () => {};

const reset = () => { window._aiStatus = undefined; window._lastStats = undefined; };

// ---- 1. Unknown state is not a problem state ----
reset();
checks.push(["nothing known → no hint", window.firstRunHint() === ""]);
checks.push(["helper never calls api()", apiCalls === 0]);

window._lastStats = { total_jobs: 12 };
window._aiStatus = { ok: true, model: "m", models: ["m"] };
checks.push(["configured user (AI ok, jobs exist) → no hint", window.firstRunHint() === ""]);

window._lastStats = {};  // stats object without the field = still unknown
window._aiStatus = undefined;
checks.push(["stats without total_jobs → no hint (never guess)", window.firstRunHint() === ""]);

// ---- 2. AI down wins over the empty-database case ----
reset();
window._aiStatus = { ok: false, base_url: "http://localhost:1234" };
window._lastStats = { total_jobs: 0 };
let hint = window.firstRunHint();
checks.push(["AI down → hint mentions the AI server", /AI server/.test(hint)]);
checks.push(["AI down → offers goAISettings()", hint.includes("goAISettings()")]);
checks.push(["AI down → does NOT offer a fetch button", !hint.includes("stageFetch()")]);

// ---- 3. Empty database (AI fine / unknown) offers a real fetch ----
reset();
window._lastStats = { total_jobs: 0 };
hint = window.firstRunHint();
checks.push(["no jobs → offers stageFetch()", hint.includes("stageFetch()")]);
checks.push(["no jobs → does not blame the AI", !/AI server/.test(hint)]);

window._aiStatus = { ok: true, model: "m", models: ["m"] };
checks.push(["no jobs + AI ok → still offers fetch", window.firstRunHint().includes("stageFetch()")]);

// ---- 4. Injection sites: copy is unchanged for configured users ----
const CLASSIC_EMPTY = '<p class="placeholder">Nothing to scout right now. '
  + '<a href="#dashboard" style="color:var(--accent)">Fetch new jobs</a> from Activity, or adjust your filters.</p>';

reset();
window._lastStats = { total_jobs: 12 };
window.renderJobs([], 0);
checks.push(["jobs list empty state unchanged when configured",
  doc.getElementById("jobs-list").innerHTML === CLASSIC_EMPTY]);

window._lastStats = { total_jobs: 0 };
window.renderJobs([], 0);
const listHtml = doc.getElementById("jobs-list").innerHTML;
checks.push(["jobs list empty state gains a fetch button on first run",
  listHtml.startsWith('<p class="placeholder">Nothing to scout right now.') && listHtml.includes("stageFetch()")]);

// Recycle bin is a different story entirely — it must never grow a hint.
doc.getElementById("filter-status").value = "deleted";
window.renderJobs([], 0);
checks.push(["Recently Deleted empty state stays bare",
  doc.getElementById("jobs-list").innerHTML === '<p class="placeholder">Recently Deleted is empty.</p>']);
doc.getElementById("filter-status").value = "";

// ---- 5. review.js queues take the AI hint ONLY ----
reset();
window._lastStats = { total_jobs: 0 };  // would trigger the fetch branch elsewhere
window.renderShortlisted({ jobs: [], total: 0 });
window.renderReviewQueue([]);
checks.push(["shortlisted empty state ignores the fetch hint",
  !doc.getElementById("shortlisted-list").innerHTML.includes("stageFetch()")]);
checks.push(["review queue empty state ignores the fetch hint",
  !doc.getElementById("review-list").innerHTML.includes("stageFetch()")]);

window._aiStatus = { ok: false, base_url: "http://x" };
window.renderShortlisted({ jobs: [], total: 0 });
window.renderReviewQueue([]);
checks.push(["shortlisted empty state warns when AI is down",
  /AI server/.test(doc.getElementById("shortlisted-list").innerHTML)]);
checks.push(["review queue warns when AI is down",
  /AI server/.test(doc.getElementById("review-list").innerHTML)]);

// ---- 6. deck.js inbox-zero card retitles only on a known-empty database ----
reset();
window._lastStats = { total_jobs: 12 };
window.renderStage();
let stageHtml = doc.getElementById("inbox-stage").innerHTML;
checks.push(["inbox-zero card keeps its title for configured users",
  stageHtml.includes("Inbox zero") && !stageHtml.includes("Welcome")]);

window._lastStats = { total_jobs: 0 };
window.renderStage();
stageHtml = doc.getElementById("inbox-stage").innerHTML;
checks.push(["inbox-zero card becomes a welcome on first run",
  stageHtml.includes("Welcome — fetch your first jobs") && !stageHtml.includes("Inbox zero")]);
checks.push(["welcome card keeps its Fetch button", stageHtml.includes("stageFetch()")]);

reset();
window.renderStage();
checks.push(["inbox-zero card unchanged when stats are unknown",
  doc.getElementById("inbox-stage").innerHTML.includes("Inbox zero")]);

checks.push(["no network traffic from any hint path", apiCalls === 0]);

const fail = report(checks);
if (fail) {
  console.error(`\ntest_first_run_hint: ${fail} check(s) failed`);
  process.exit(1);
}
console.log("\ntest_first_run_hint.js: all checks passed");
process.exit(0);
