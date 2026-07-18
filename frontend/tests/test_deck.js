// Phase 3 "Command Deck" regression test: the ⌘K palette registry filters and
// escapes hostile input, the kanban drag map exposes EXACTLY the five allowed
// transitions (each hitting the right endpoint + payload), the stage keyboard
// verdicts PATCH the right status, and the layout toggle flips the body class
// with 'deck' as the default layout.
//
// Same jsdom-as-one-eval-unit style as test_ledger.js / test_foundry.js: the
// production scripts are eval'd unmodified as ONE unit so top-level function
// declarations become window globals; internal const/let are exercised only
// through their observable effects (stubbed api() calls, DOM, body class).
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
     <div id="pipeline-funnel"></div>
     <div id="theme-toggle"></div>
     <section id="jobs"><div class="jobs-split-pane"></div><div id="inbox-stage" class="inbox-stage"></div></section>
     <section id="review"><div id="pipeline-board" class="pipeline-board"></div></section>
   </body></html>`,
  { runScripts: "dangerously", pretendToBeVisual: true, url: "http://localhost:8888/", virtualConsole }
);
const { window } = dom;
const doc = window.document;

// One eval unit, order matches index.html (core → dashboard → jobs → review →
// jobs-actions → deck).
const SCRIPTS = ["core.js", "dashboard.js", "jobs.js", "review.js", "jobs-actions.js", "deck.js"];
window.eval(
  SCRIPTS.map((f) => fs.readFileSync(path.join(JS_DIR, f), "utf8")).join("\n;\n")
);

const checks = [];

// ---- Shared stubs so the runners don't hit the network / re-render heavily ----
const calls = [];
window.api = (url, opts) => { calls.push({ url, opts }); return Promise.resolve({ jobs: [], total: 0 }); };
// core.js's DOMContentLoaded init fires once this async test yields to the event
// loop; its handleHash()→enterInbox()→loadJobs() would hit the absent classic
// filter DOM. Neutralise the loaders it can reach so init stays quiet.
window.loadJobs = () => Promise.resolve();
window.toast = () => {};
window.renderBoard = () => {};          // runDeckDrop re-renders — no-op it
window.refreshFunnelCounts = () => {};  // "
let confirmCount = 0;
window.appConfirm = () => { confirmCount++; return Promise.resolve(true); };

const statusOf = (c) => { try { return JSON.parse(c.opts.body).status; } catch (e) { return null; } };
const methodOf = (c) => (c.opts && c.opts.method) || "GET";

// ===================================================================
// 1. Palette registry — filtering + escaping (hostile query AND label)
// ===================================================================

// Substring filtering keeps the matching command and drops the rest.
const inboxHits = window.buildPalette("inbox");
checks.push(["substring filter keeps 'Inbox'", inboxHits.some((i) => i.label === "Inbox")]);
checks.push(["substring filter drops non-matches", !inboxHits.some((i) => i.label === "Applied")]);

// Subsequence fallback: 'tlr' is not a substring of "Tailor résumés" but is an
// ordered subsequence (t…l…r).
const subseq = window.buildPalette("tlr");
checks.push(["subsequence match finds 'Tailor résumés'", subseq.some((i) => i.label === "Tailor résumés")]);

// Empty query returns the whole registry and NO fallback row.
const all = window.buildPalette("");
checks.push(["empty query returns every registry item", all.length >= 20]);
checks.push(["empty query has no fallback search row", !all.some((i) => i.group === "Jobs")]);

// A query that matches nothing still yields the fallback "Search jobs for …" row.
const nomatch = window.buildPalette("zzqqxnope");
checks.push(["no-match query yields exactly the fallback row", nomatch.length === 1 && nomatch[0].group === "Jobs"]);

// Keyword aliases match even when the label doesn't ('board' → Pipeline).
checks.push(["keyword alias matches (board → Pipeline)", window.buildPalette("board").some((i) => i.label === "Pipeline")]);

// Escaping: a hostile QUERY is echoed into the fallback label but rendered inert.
const hostileQ = window.buildPalette('<img src=x onerror=alert(1)>');
const fallback = hostileQ.find((i) => i.group === "Jobs");
const probe = doc.createElement("div");
probe.innerHTML = fallback.html;
checks.push(["hostile query injects no <img> element", probe.querySelectorAll("img").length === 0]);

// Escaping: a hostile LABEL passed straight to paletteHighlight is escaped, and
// the matched slice is still wrapped in <em>.
const hostileLabel = window.paletteHighlight("<img src=x onerror=alert(1)>", "img");
const probe2 = doc.createElement("div");
probe2.innerHTML = hostileLabel;
checks.push(["paletteHighlight injects no <img> element", probe2.querySelectorAll("img").length === 0]);
checks.push(["paletteHighlight wraps the match in <em>", probe2.querySelectorAll("em").length >= 1]);
checks.push(["paletteHighlight preserves the literal text", probe2.textContent === "<img src=x onerror=alert(1)>"]);

// ===================================================================
// 2. Allowed-transition map — exactly 5, each → the right endpoint/payload
// ===================================================================
checks.push(["there are exactly 5 allowed transitions", window.allDeckTransitions().length === 5]);

async function assertDrop(from, to, id, verify) {
  calls.length = 0; confirmCount = 0;
  const ok = await window.runDeckDrop(from, to, id);
  return verify(ok, calls, confirmCount);
}

// ===================================================================
// 3–4. run at the bottom inside an async IIFE (jsdom promises)
// ===================================================================
(async () => {
  // shortlisted → tailoring : POST /api/jobs/{id}/tailor
  checks.push(["shortlisted→tailoring maps to tailor endpoint", await assertDrop("shortlisted", "tailoring", "j1",
    (ok, cs) => ok && cs.some((c) => c.url === "/api/jobs/j1/tailor" && methodOf(c) === "POST"))]);

  // shortlisted → applied : PATCH /api/jobs/{id}/status {status:'manual'} + confirm
  checks.push(["shortlisted→applied maps to job status manual (confirmed)", await assertDrop("shortlisted", "applied", "j2",
    (ok, cs, cf) => ok && cf === 1 && cs.some((c) => c.url === "/api/jobs/j2/status" && methodOf(c) === "PATCH" && statusOf(c) === "manual"))]);

  // pending → applied : PATCH /api/applications/{id}/status {status:'applied'}
  checks.push(["pending→applied maps to application status applied", await assertDrop("pending", "applied", "a1",
    (ok, cs) => ok && cs.some((c) => c.url === "/api/applications/a1/status" && methodOf(c) === "PATCH" && statusOf(c) === "applied"))]);

  // needs-attention → pending : POST /api/applications/{id}/requeue
  checks.push(["needs-attention→pending maps to requeue", await assertDrop("needs-attention", "pending", "a2",
    (ok, cs) => ok && cs.some((c) => c.url === "/api/applications/a2/requeue" && methodOf(c) === "POST"))]);

  // shortlisted → pass : PATCH /api/jobs/{id}/status {status:'passed'}
  checks.push(["shortlisted→pass maps to job status passed", await assertDrop("shortlisted", "pass", "j3",
    (ok, cs) => ok && cs.some((c) => c.url === "/api/jobs/j3/status" && methodOf(c) === "PATCH" && statusOf(c) === "passed"))]);

  // A disallowed pair is refused: no transition, no api call.
  checks.push(["disallowed target has no transition", window.findDeckTransition("applied", "shortlisted") === null]);
  const refusedOk = await assertDrop("applied", "shortlisted", "x9", (ok, cs) => ok === false && cs.length === 0);
  checks.push(["disallowed drop makes no api call and returns false", refusedOk]);

  // ===================================================================
  // 5. Stage keyboard verdicts call the right endpoint on the top card
  // ===================================================================
  const seed = () => window.stageSetJobs([
    { id: "s1", title: "Top role", status: "discovered", url: "https://x.test/1" },
    { id: "s2", title: "Next role", status: "discovered", url: "https://x.test/2" },
  ], 8);

  seed(); calls.length = 0;
  window.stagePass();  // PATCH fires synchronously (before the await), on the top card
  checks.push(["stage pass PATCHes status=passed on the top card", calls.some((c) => c.url === "/api/jobs/s1/status" && statusOf(c) === "passed")]);

  seed(); calls.length = 0;
  window.stageShortlist();
  checks.push(["stage shortlist PATCHes status=shortlisted on the top card", calls.some((c) => c.url === "/api/jobs/s1/status" && statusOf(c) === "shortlisted")]);

  seed(); calls.length = 0;
  window.stageShortlistTailor();
  await new Promise((r) => setTimeout(r, 0));  // let the awaited PATCH resolve so the tailor POST fires
  checks.push(["stage T shortlists the top card", calls.some((c) => c.url === "/api/jobs/s1/status" && statusOf(c) === "shortlisted")]);
  checks.push(["stage T then tailors the top card", calls.some((c) => c.url === "/api/jobs/s1/tailor" && methodOf(c) === "POST")]);

  // ===================================================================
  // 6. Layout toggle applies the body class and defaults to 'deck'
  // ===================================================================
  window.localStorage.removeItem("jobsmith_layout");
  checks.push(["default layout is deck", window.getLayout() === "deck" && window.isDeckLayout() === true]);
  checks.push(["explicit classic choice is honored", (window.localStorage.setItem("jobsmith_layout", "classic"), window.getLayout() === "classic")]);
  window.localStorage.removeItem("jobsmith_layout");
  window.handleHash = () => {};  // setLayout re-renders via handleHash — no-op it here
  window.setLayout("deck");
  checks.push(["setLayout('deck') adds the layout-deck body class", doc.body.classList.contains("layout-deck")]);
  checks.push(["getLayout reflects the deck choice", window.getLayout() === "deck"]);
  window.setLayout("classic");
  checks.push(["setLayout('classic') removes the body class", !doc.body.classList.contains("layout-deck")]);

  const fail = report(checks);
  if (fail) {
    console.error(`\ntest_deck: ${fail} check(s) failed`);
    process.exit(1);
  }
  console.log("\ntest_deck.js: all checks passed");
  process.exit(0);
})();
