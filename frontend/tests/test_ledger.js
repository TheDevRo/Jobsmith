// Phase 1 "Ledger" regression test: filter-chip building escapes HTML, and the
// verdict undo stack restores the previous status (bounded at 10).
//
// Mirrors test_safe_href.js: jsdom + the production scripts eval'd unmodified as
// ONE unit (so top-level const/function bindings are shared across files, and
// jobs.js can call review.js's globals). Inline handlers rely on
// runScripts:"dangerously"; a real origin is required because core.js touches
// localStorage on load. Top-level const/let are lexical globals that aren't
// visible to a later indirect eval, so the undo stack + funnel counts are
// exercised through their observable effects (API PATCH calls, toasts, DOM),
// never by reading the internal state directly.
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

// core.js's DOMContentLoaded handler references helpers from scripts we don't
// load (sessions.js) and browser globals jsdom lacks (fetch); those listener
// errors are harmless to this test, so keep them off the console.
const virtualConsole = new VirtualConsole();

const dom = new JSDOM(
  `<!DOCTYPE html><html><body>
     <div id="app-banners"></div>
     <div id="toast-container"></div>
     <input type="text" id="filter-search">
     <select id="filter-sort"><option value="fit_score-desc" selected>x</option></select>
     <select id="filter-source"><option value="" selected></option><option value="linkedin">LinkedIn</option></select>
     <select id="filter-status"><option value="" selected></option><option value="applied">Applied</option></select>
     <input type="text" id="filter-location">
     <input type="text" id="filter-company">
     <input type="checkbox" id="filter-remote">
     <input type="checkbox" id="filter-easy-apply">
     <input type="range" id="filter-score" min="0" max="100" value="0">
     <span id="score-val">0</span>
     <input type="range" id="filter-salary" min="0" max="300000" value="0">
     <span id="salary-val">0</span>
     <input type="date" id="filter-date-from">
     <input type="date" id="filter-date-to">
     <input type="checkbox" id="filter-include-estimated">
     <input type="hidden" id="filter-max-score" value="">
     <input type="hidden" id="filter-unscored-only" value="">
     <div id="filter-advanced" style="display:none"></div>
     <button id="filter-toggle"></button>
     <div id="filter-chips"></div>
     <div id="jobs-list"></div>
     <div id="jobs-pagination"></div>
     <input type="checkbox" id="select-all-jobs">
     <span id="selected-count"></span>
     <div id="job-detail-pane"></div>
     <div id="pipeline-funnel"></div>
     <div id="theme-toggle"></div>
   </body></html>`,
  { runScripts: "dangerously", pretendToBeVisual: true, url: "http://localhost:8888/", virtualConsole }
);
const { window } = dom;
const doc = window.document;

// One eval unit, order matches index.html (core → jobs → review → jobs-actions).
const SCRIPTS = ["core.js", "jobs.js", "review.js", "jobs-actions.js"];
window.eval(
  SCRIPTS.map((f) => fs.readFileSync(path.join(JS_DIR, f), "utf8")).join("\n;\n")
);

const checks = [];

// ---- buildFilterChips(): active non-default filters only ----
doc.getElementById("filter-search").value = "platform";
doc.getElementById("filter-remote").checked = true;
doc.getElementById("filter-score").value = "70";
doc.getElementById("filter-source").value = "linkedin";

const byKey = Object.fromEntries(window.buildFilterChips().map((c) => [c.key, c.label]));
checks.push(["search chip present", byKey.search === "“platform”"]);
checks.push(["remote chip present", byKey.remote === "Remote only"]);
checks.push(["score chip formats threshold", byKey.score === "Score ≥ 70"]);
checks.push(["source chip uses human label", byKey.source === "Source: LinkedIn"]);
checks.push([
  "no chip for a default/empty filter (company)",
  !("company" in byKey),
]);

// ---- renderFilterChips() escapes HTML in labels ----
doc.getElementById("filter-search").value = '<img src=x onerror=alert(1)>';
window.renderFilterChips();
const chipsEl = doc.getElementById("filter-chips");
const chipsHtml = chipsEl.innerHTML;
// The security property: the hostile value is rendered as inert TEXT, not parsed
// into a live element. (It may appear raw inside an attribute value on
// re-serialization — that's inert — so assert on the DOM, not the HTML string.)
checks.push(["chip label injects no element", chipsEl.querySelectorAll("img").length === 0]);
const firstChipText = chipsEl.querySelector(".fchip span").textContent;
checks.push([
  "chip label rendered as escaped text",
  firstChipText === "“<img src=x onerror=alert(1)>”",
]);
checks.push(["+ Filter chip rendered", chipsHtml.includes("addf")]);
checks.push(["sort indicator rendered", chipsHtml.includes("Sort:")]);

// ---- resetFilter() clears one input and reloads ----
let loadCalls = 0;
window.loadJobs = () => { loadCalls++; return Promise.resolve(); };
doc.getElementById("filter-remote").checked = true;
window.resetFilter("remote");
checks.push(["resetFilter clears its own input", doc.getElementById("filter-remote").checked === false]);
checks.push(["resetFilter triggers a reload", loadCalls === 1]);

// ---- Funnel rendering (segment per view, counts, exactly one active) ----
window._setFunnelCount("shortlisted", 4); // renders too
const funnelHtml = doc.getElementById("pipeline-funnel").innerHTML;
checks.push(["funnel renders one segment per view (5)", (funnelHtml.match(/class="fseg/g) || []).length === 5]);
checks.push(["funnel segment shows its count", funnelHtml.includes(">4<")]);
checks.push(["zero-count segment gets .empty", funnelHtml.includes(" empty")]);
checks.push(["exactly one active segment", (funnelHtml.match(/aria-selected="true"/g) || []).length === 1]);

// ---- Verdict undo stack (observed via API PATCH calls + restore toast) ----
const apiCalls = [];
const toasts = [];
window.api = (url, opts) => { apiCalls.push({ url, opts }); return Promise.resolve({}); };
window.loadJobs = () => Promise.resolve();
window.toast = (m) => toasts.push(m);
window.selectJob = () => {};

const statusOf = (c) => { try { return JSON.parse(c.opts.body).status; } catch (e) { return null; } };

(async () => {
  // A pass verdict, then undo → PATCHes the previous status ('discovered') back
  // and toasts the captured title.
  window._currentJobs = { j1: { id: "j1", title: "Senior Platform Engineer", status: "discovered", url: "https://x.test/1" } };
  apiCalls.length = 0; toasts.length = 0;
  await window.passJob("j1");
  checks.push(["pass PATCHes status=passed", apiCalls.some((c) => c.url === "/api/jobs/j1/status" && statusOf(c) === "passed")]);
  apiCalls.length = 0;
  await window.undoVerdict();
  checks.push(["undo restores the previous status", apiCalls.some((c) => c.url === "/api/jobs/j1/status" && statusOf(c) === "discovered")]);
  checks.push(["undo toasts the captured job title", toasts.includes("Restored Senior Platform Engineer")]);

  // Undo with an empty stack is a no-op (no PATCH).
  // Drain any residual entries first, then confirm the next undo does nothing.
  for (let i = 0; i < 20; i++) await window.undoVerdict();
  apiCalls.length = 0;
  await window.undoVerdict();
  checks.push(["undo on an empty stack does nothing", apiCalls.length === 0]);

  // The stack is bounded: 14 verdicts, but only the last 10 are undoable.
  for (let i = 0; i < 14; i++) {
    window._currentJobs["k" + i] = { id: "k" + i, title: "T" + i, status: "discovered" };
    await window.shortlistJob("k" + i);
  }
  let restores = 0;
  for (let i = 0; i < 14; i++) {
    apiCalls.length = 0;
    await window.undoVerdict();
    if (apiCalls.some((c) => /\/status$/.test(c.url))) restores++;
  }
  checks.push(["undo stack is capped at 10", restores === 10]);

  const fail = report(checks);
  if (fail) {
    console.error(`\ntest_ledger: ${fail} check(s) failed`);
    process.exit(1);
  }
  console.log("\ntest_ledger.js: all checks passed");
  process.exit(0);
})();
