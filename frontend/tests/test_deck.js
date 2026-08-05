// Phase 3 "Command Deck" regression test: the ⌘K palette registry filters and
// escapes hostile input, the kanban drag map exposes EXACTLY the five allowed
// transitions (each hitting the right endpoint + payload), the stage keyboard
// verdicts PATCH the right status, and the two per-view toggles (Inbox
// cards⇄list, Pipeline board⇄table) each set a state class on their own
// section, with the deck rendering as the default on both.
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
     <div id="pipeline-filter-chip" hidden></div>
     <div id="theme-toggle"></div>
     <section id="jobs" class="view-cards"><div class="jobs-split-pane"></div><div id="inbox-stage" class="inbox-stage"></div>
       <button id="inbox-view-toggle">List view</button></section>
     <section id="review" class="view-board"><div id="pipeline-board" class="pipeline-board"></div>
       <div class="review-tab-bar" id="review-tab-bar"></div>
       <button id="pipeline-view-toggle">Table view</button></section>
   </body></html>`,
  { runScripts: "dangerously", pretendToBeVisual: true, url: "http://localhost:8888/", virtualConsole }
);
const { window } = dom;
const doc = window.document;

// One eval unit, order matches index.html (core → dashboard → job-actions →
// jobs → review → jobs-actions → deck).
const SCRIPTS = ["pipeline-stages.js", "core.js", "dashboard.js", "job-actions.js", "jobs.js", "review.js", "jobs-actions.js", "deck.js"];
window.eval(
  SCRIPTS.map((f) => fs.readFileSync(path.join(JS_DIR, f), "utf8")).join("\n;\n")
);

const checks = [];

// ---- Shared stubs so the runners don't hit the network / re-render heavily ----
const calls = [];
const _realRenderBoard = window.renderBoard;   // section 10 drives the real one
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
  // 6. Per-view toggles: each screen owns its own rendering, both defaulting
  //    to the deck rendering, and each sets a state class on its OWN section
  //    (there is no app-wide layout mode / body class any more).
  // ===================================================================
  const jobsSection = doc.getElementById("jobs");
  const reviewSection = doc.getElementById("review");
  // enterInbox/enterReview run the real loaders; stub the heavy ones out for
  // this section only (section 8 below drives the REAL loadStage).
  const _realLoadStage = window.loadStage;
  const _realSwitchReviewView = window.switchReviewView;
  window.loadStage = () => Promise.resolve();
  window.renderBoard = () => {};
  window.switchReviewView = () => {};

  window.localStorage.removeItem("jobsmith_inbox_view");
  checks.push(["default Inbox view is cards", window.getInboxView() === "cards"]);
  checks.push(["explicit Inbox list choice is honored",
    (window.localStorage.setItem("jobsmith_inbox_view", "list"), window.getInboxView() === "list")]);
  checks.push(["legacy 'stage' value still reads as cards",
    (window.localStorage.setItem("jobsmith_inbox_view", "stage"), window.getInboxView() === "cards")]);

  window.setInboxView("cards");
  checks.push(["setInboxView('cards') marks #jobs.view-cards",
    jobsSection.classList.contains("view-cards") && !jobsSection.classList.contains("view-list")]);
  checks.push(["cards view makes the triage stage active", window.isInboxStageActive() === true]);
  checks.push(["cards toggle offers the list", doc.getElementById("inbox-view-toggle").textContent === "List view"]);
  window.setInboxView("list");
  checks.push(["setInboxView('list') marks #jobs.view-list",
    jobsSection.classList.contains("view-list") && !jobsSection.classList.contains("view-cards")]);
  checks.push(["list view stands the triage stage down", window.isInboxStageActive() === false]);
  checks.push(["list toggle offers the cards", doc.getElementById("inbox-view-toggle").textContent === "Card view"]);
  window.toggleInboxView();
  checks.push(["toggleInboxView flips list back to cards", window.getInboxView() === "cards"]);
  checks.push(["no app-wide layout body class is set", !doc.body.classList.contains("layout-deck")]);

  window.localStorage.removeItem("jobsmith_pipeline_view");
  checks.push(["default Pipeline view is board", window.getPipelineView() === "board"]);
  checks.push(["explicit Pipeline table choice is honored",
    (window.localStorage.setItem("jobsmith_pipeline_view", "table"), window.getPipelineView() === "table")]);

  window.setPipelineView("board");
  checks.push(["setPipelineView('board') marks #review.view-board",
    reviewSection.classList.contains("view-board") && !reviewSection.classList.contains("view-table")]);
  checks.push(["board view is the active board mode", window.isBoardModeActive() === true]);
  checks.push(["board toggle offers the table", doc.getElementById("pipeline-view-toggle").textContent === "Table view"]);
  window.setPipelineView("table");
  checks.push(["setPipelineView('table') marks #review.view-table",
    reviewSection.classList.contains("view-table") && !reviewSection.classList.contains("view-board")]);
  checks.push(["table view stands the board down", window.isBoardModeActive() === false]);
  checks.push(["table toggle offers the board", doc.getElementById("pipeline-view-toggle").textContent === "Board view"]);
  window.togglePipelineView();
  checks.push(["togglePipelineView flips table back to board", window.getPipelineView() === "board"]);

  // The two toggles are independent — all four cells of the matrix are reachable.
  window.setInboxView("list"); window.setPipelineView("board");
  checks.push(["inbox=list + pipeline=board is a reachable cell",
    jobsSection.classList.contains("view-list") && reviewSection.classList.contains("view-board")]);
  window.setInboxView("cards"); window.setPipelineView("table");
  checks.push(["inbox=cards + pipeline=table is a reachable cell",
    jobsSection.classList.contains("view-cards") && reviewSection.classList.contains("view-table")]);

  // The palette exposes both toggles (the old single layout command is gone).
  const paletteLabels = window.buildPalette("").map((i) => i.label);
  checks.push(["palette offers the Inbox view toggle", paletteLabels.includes("Toggle Inbox view (cards / list)")]);
  checks.push(["palette offers the Pipeline view toggle", paletteLabels.includes("Toggle Pipeline view (board / table)")]);
  checks.push(["palette no longer offers a global layout toggle",
    !paletteLabels.some((l) => /Deck \/ Classic/.test(l))]);

  window.loadStage = _realLoadStage;
  window.switchReviewView = _realSwitchReviewView;

  // ===================================================================
  // 7. Job peek modal — clicking a posting pops a modal in place and NEVER
  //    flips the inbox view pref or navigates (the "stuck in classic" bug).
  // ===================================================================
  window.setPipelineView("board");
  window.localStorage.setItem("jobsmith_inbox_view", "cards");
  calls.length = 0;
  const hashBefore = window.location.hash;
  window.api = (url) => {
    calls.push({ url });
    if (url === "/api/jobs/j7") {
      return Promise.resolve({
        id: "j7", title: "Modal role <b>x</b>", company: "Acme", status: "shortlisted",
        url: "https://x.test/7", description: "desc",
        application: { id: "app7", status: "pending_review" },
      });
    }
    return Promise.resolve({ jobs: [], total: 0 });
  };
  await window.boardOpenJob("j7");
  await new Promise((r) => setTimeout(r, 0));
  const overlay = doc.getElementById("job-modal-overlay");
  checks.push(["boardOpenJob opens the peek modal", !!overlay]);
  checks.push(["modal fetched the single-job endpoint", calls.some((c) => c.url === "/api/jobs/j7")]);
  checks.push(["modal renders the shared detail body", !!(overlay && overlay.querySelector(".detail-title"))]);
  checks.push(["modal escapes the job title", !(overlay && overlay.querySelector(".detail-title b"))]);
  checks.push(["nested application surfaces View Application", !!(overlay && overlay.innerHTML.includes("View Application"))]);
  // Phase 0 unified the Apply Assist rule (job-actions.js): offered whenever
  // the posting has a URL, in every context — the modal no longer has its own
  // "always" override and the classic pane no longer gates on apply_type.
  // j7 has a url, so the modal still offers it.
  checks.push(["modal offers Apply Assist for a job with a URL", !!(overlay && overlay.innerHTML.includes("Apply Assist"))]);
  checks.push(["classic pane offers Apply Assist for the same job regardless of apply_type",
    window.buildJobDetailHtml({ id: "j7", title: "t", source: "s", url: "https://x.test/7" }).includes("Apply Assist")
      && window.buildJobDetailHtml({ id: "j7", title: "t", source: "s", url: "https://x.test/7", apply_type: "external" }).includes("Apply Assist")]);
  checks.push(["no URL → no Apply Assist anywhere",
    !window.buildJobDetailHtml({ id: "j7", title: "t", source: "s" }).includes("Apply Assist")]);
  // A soft-deleted posting can't be deleted again (the API updates zero rows),
  // so the detail body swaps Delete for Restore + Erase Permanently. Its own
  // 'deleted' status must also win over any application status it still carries.
  const deletedBody = window.buildJobDetailHtml({
    id: "jd", title: "t", source: "s", status: "deleted", app_status: "pending_review",
  });
  checks.push(["deleted job offers Restore", deletedBody.includes("Restore to Inbox")]);
  checks.push(["deleted job offers Erase Permanently", deletedBody.includes("Erase Permanently")]);
  checks.push(["deleted job hides the Delete button", !deletedBody.includes("deleteSingleJob")]);
  checks.push(["deleted job hides Tailor/Mark Applied", !deletedBody.includes("tailorJob") && !deletedBody.includes("markApplied")]);
  checks.push(["deleted status wins over the application status", deletedBody.includes("pill-deleted")]);
  const liveBody = window.buildJobDetailHtml({ id: "jl", title: "t", source: "s", status: "shortlisted" });
  checks.push(["live job still offers Delete", liveBody.includes("deleteSingleJob") && !liveBody.includes("Erase Permanently")]);

  checks.push(["boardOpenJob leaves the inbox view pref alone", window.localStorage.getItem("jobsmith_inbox_view") === "cards"]);
  checks.push(["boardOpenJob does not navigate", window.location.hash === hashBefore]);
  checks.push(["isJobModalOpen reports open", window.isJobModalOpen() === true]);
  window.closeJobModal();
  checks.push(["closeJobModal removes the overlay", !doc.getElementById("job-modal-overlay") && window.isJobModalOpen() === false]);

  // App cards route through the same modal via their job id.
  calls.length = 0;
  await window.boardOpenApp("pending", "app7", "j7");
  await new Promise((r) => setTimeout(r, 0));
  checks.push(["boardOpenApp with a job id opens the peek modal", !!doc.getElementById("job-modal-overlay")]);
  window.closeJobModal();

  // ===================================================================
  // 8. Stage fetch honors the synced Inbox prefs (sort mapping + pay gate),
  //    and is byte-identical to the legacy query when nothing is set.
  // ===================================================================
  // Drive through loadStage(), which loads /api/config then fetches /api/jobs.
  // A stub returns the config; we assert the resulting /api/jobs query string.
  async function stageFetchUrlFor(cfg) {
    window.api = (url) => {
      calls.push({ url });
      if (url === "/api/config") return Promise.resolve(cfg);
      return Promise.resolve({ jobs: [], total: 0 });
    };
    calls.length = 0;
    await window.loadStage();
    await new Promise((r) => setTimeout(r, 0));
    return calls.map((c) => c.url).find((u) => u.startsWith("/api/jobs"));
  }

  // Defaults (no inbox prefs, no floor): unchanged from the pre-feature string.
  const defUrl = await stageFetchUrlFor({});
  checks.push(["default stage fetch is the legacy query",
    defUrl === "/api/jobs?status=discovered&sort_by=fit_score&sort_dir=desc&limit=30"]);

  // Salary sort + a floor + require_stated_pay -> mapped sort and pay params.
  const salUrl = await stageFetchUrlFor({ inbox: { sort: "salary", require_stated_pay: true }, search: { min_salary: 90000 } });
  checks.push(["salary sort maps to sort_by=salary desc", salUrl.includes("sort_by=salary") && salUrl.includes("sort_dir=desc")]);
  checks.push(["floor set adds pay_floor", salUrl.includes("pay_floor=90000")]);
  checks.push(["require_stated_pay passed with a floor", salUrl.includes("require_stated_pay=true")]);

  // Company sort maps to ascending.
  const coUrl = await stageFetchUrlFor({ inbox: { sort: "company" }, search: {} });
  checks.push(["company sort maps to sort_by=company asc", coUrl.includes("sort_by=company") && coUrl.includes("sort_dir=asc")]);

  // require_stated_pay WITHOUT a floor is ignored (contract): no pay params.
  const noFloorUrl = await stageFetchUrlFor({ inbox: { sort: "newest", require_stated_pay: true }, search: { min_salary: 0 } });
  checks.push(["newest sort maps to date_discovered desc", noFloorUrl.includes("sort_by=date_discovered") && noFloorUrl.includes("sort_dir=desc")]);
  checks.push(["require_stated_pay ignored without a floor", !noFloorUrl.includes("pay_floor") && !noFloorUrl.includes("require_stated_pay")]);

  // ===================================================================
  // 9. Pipeline card delete — one click, undo instead of a confirm dialog.
  // ===================================================================
  const jobCard = window._jobKcardHtml({ id: "kj1", title: "Role", company: "Acme" }, "shortlisted");
  checks.push(["job card renders the delete affordance", jobCard.includes("kdel-btn") && jobCard.includes("boardDeleteCard('kj1'")]);
  checks.push(["job card keeps the ⋯ menu", jobCard.includes("kmenu-btn")]);

  // Application cards delete the posting behind them, not the application row.
  const appCard = window._appKcardHtml({ id: "app9", job_id: "kj9", title: "Role" }, "applied");
  checks.push(["app card deletes its underlying job", appCard.includes("boardDeleteCard('kj9'")]);
  const orphanCard = window._appKcardHtml({ id: "app0", title: "Role" }, "applied");
  checks.push(["app card with no job_id has no delete button", !orphanCard.includes("kdel-btn")]);

  // The runner: DELETE, then an undo toast whose action PATCHes the job back to
  // the status it held (so the card returns to its column, not the Inbox).
  const toasts = [];
  window.toastAction = (msg, label, onAction) => { toasts.push({ msg, label, onAction }); return null; };
  calls.length = 0;
  window.api = (url, opts) => {
    calls.push({ url, opts });
    if ((opts || {}).method === "DELETE") return Promise.resolve({ deleted: 1, previous_status: "shortlisted" });
    return Promise.resolve({});
  };
  const confirmBefore = confirmCount;
  await window.boardDeleteCard("kj1", null);
  const delCall = calls.find((c) => methodOf(c) === "DELETE");
  checks.push(["delete hits the single-job endpoint", !!delCall && delCall.url === "/api/jobs/kj1"]);
  checks.push(["delete does NOT open a confirm dialog", confirmCount === confirmBefore]);
  checks.push(["delete offers an Undo toast", toasts.length === 1 && toasts[0].label === "Undo"]);

  calls.length = 0;
  await toasts[0].onAction();
  const undoCall = calls.find((c) => methodOf(c) === "PATCH");
  checks.push(["undo PATCHes the previous status back", !!undoCall
    && undoCall.url === "/api/jobs/kj1/status" && statusOf(undoCall) === "shortlisted"]);

  // Delete is reachable without a pointer: the ⋯ menu carries it, and a column
  // with no legal moves still opens a menu instead of refusing.
  window.boardCardMenu({ currentTarget: doc.body }, "applied", "app9", "kj9");
  const menu = doc.getElementById("kmenu");
  checks.push(["card menu offers Delete posting", !!menu && menu.innerHTML.includes("Delete posting")]);
  window._closeCardMenu();

  // ===================================================================
  // 10. Phase 2 — the funnel is the ONE Pipeline summary/filter.
  //     (a) every stage label comes from PIPELINE_STAGES,
  //     (b) a funnel click filters the board (and toggles off),
  //     (c) stage jumps route the same way in both views.
  // ===================================================================
  window.renderBoard = _realRenderBoard;
  window.api = (url) => {
    calls.push({ url });
    if (url === "/api/applications/in-progress") return Promise.resolve({ in_progress: [], needs_attention: [] });
    if (url.startsWith("/api/applications/")) return Promise.resolve([]);
    if (url === "/api/operations/status") return Promise.resolve({});
    return Promise.resolve({ jobs: [], total: 0 });
  };

  // --- (a) single source of stage labels ---
  const stageLabels = window.pipelineStages().map((s) => s.label);
  checks.push(["board columns are the board stages, in order",
    JSON.stringify(window.boardStages().map((s) => s.key))
      === JSON.stringify(["shortlisted", "tailoring", "pending", "applied", "needs-attention"])]);
  checks.push(["funnel stages are the five funnel segments, in order",
    JSON.stringify(window.funnelStages().map((s) => s.key))
      === JSON.stringify(["shortlisted", "pending", "applied", "failed", "in-progress"])]);
  checks.push(["stageLabel resolves a column key", window.stageLabel("pending") === "Ready to Review"]);
  checks.push(["stageLabel keeps a word for the Pass verdict", window.stageLabel("pass") === "Pass"]);
  checks.push(["classic tab name maps back to a stage key", window.stageKeyForTab("submitted") === "applied"]);

  window.setPipelineView("board");
  await window.renderBoard();
  await new Promise((r) => setTimeout(r, 0));
  const boardHtml = doc.getElementById("pipeline-board").innerHTML;
  checks.push(["column heads render the constant's labels",
    window.boardStages().every((s) => boardHtml.includes(`<b>${s.label}</b>`))]);
  const funnelHtml = doc.getElementById("pipeline-funnel").innerHTML;
  checks.push(["funnel segments render the constant's labels",
    window.funnelStages().every((s) => funnelHtml.includes(`<span>${s.label}</span>`))]);
  checks.push(["funnel segments carry a stage tooltip",
    window.funnelStages().every((s) => funnelHtml.includes(s.desc.slice(0, 24)))]);
  checks.push(["funnel segments are focusable buttons",
    doc.querySelectorAll("#pipeline-funnel button.fseg").length === 5]);
  window.renderReviewTabs();
  const tabHtml = doc.getElementById("review-tab-bar").innerHTML;
  checks.push(["stage tabs render the constant's labels",
    window.pipelineStages().filter((s) => s.tab).every((s) => tabHtml.includes(`>${s.label}`))]);
  checks.push(["stage tabs keep their deep-link ids",
    ["shortlisted", "pending", "submitted", "failed", "in-progress"]
      .every((t) => !!doc.getElementById(`review-tab-${t}`))]);
  checks.push(["the move menu labels its destination from the constant",
    window._transitionMenuLabel({ to: "pending", label: "requeues for review" })
      === "Move to Ready to Review — requeues for review"]);
  checks.push(["the board legend is generated from the transition map",
    window._boardLegendText().startsWith("Shortlisted → Tailoring/Applied/Pass")]);
  // No stage label is hard-coded twice: the labels only exist in the stage map.
  checks.push(["stage labels are unique strings in the map", new Set(stageLabels).size === stageLabels.length]);

  // --- (b) board filter: apply, toggle off, and the ✕ chip ---
  const board = doc.getElementById("pipeline-board");
  const colClasses = (k) => doc.querySelector(`.kcol[data-col="${k}"]`).className;
  window.pipelineSegmentClick("applied");
  checks.push(["clicking a segment filters the board", window.getBoardFilter() === "applied"]);
  checks.push(["the board is marked filtered", board.classList.contains("board-filtered")]);
  checks.push(["the chosen column is focused", colClasses("applied").includes("kcol-focus")]);
  checks.push(["other columns are dimmed", colClasses("shortlisted").includes("kcol-dim")
    && !colClasses("applied").includes("kcol-dim")]);
  checks.push(["the active segment is marked pressed",
    doc.getElementById("pipeline-funnel").innerHTML.includes('aria-pressed="true"')]);
  checks.push(["a clear-filter chip appears", !doc.getElementById("pipeline-filter-chip").hidden
    && doc.getElementById("pipeline-filter-chip").innerHTML.includes("Applied")]);
  window.pipelineSegmentClick("applied");
  checks.push(["clicking the active segment clears the filter", window.getBoardFilter() === null]);
  checks.push(["clearing un-dims every column", !colClasses("shortlisted").includes("kcol-dim")
    && !board.classList.contains("board-filtered")]);
  checks.push(["the chip goes away", doc.getElementById("pipeline-filter-chip").hidden]);

  // Failed and In Progress share the Needs Attention column but are distinct
  // segments — switching between them must not read as "toggle off".
  window.pipelineSegmentClick("failed");
  checks.push(["Failed filters the Needs Attention column",
    window.getBoardFilter() === "needs-attention" && window.getBoardFilterStage() === "failed"]);
  window.pipelineSegmentClick("in-progress");
  checks.push(["In Progress keeps the column and moves the segment",
    window.getBoardFilter() === "needs-attention" && window.getBoardFilterStage() === "in-progress"]);
  window.clearBoardFilter();

  // Re-rendering the board must not silently drop an active filter.
  window.setBoardFilter("pending", "pending");
  await window.renderBoard();
  checks.push(["a board re-render keeps the filter applied",
    colClasses("pending").includes("kcol-focus") && colClasses("applied").includes("kcol-dim")]);
  window.clearBoardFilter();

  // --- (c) one rule for stage jumps, both views ---
  const _realEnterReview = window.enterReview;
  window.enterReview = () => {};              // don't let hashchange reset state
  window.location.hash = "review";
  await new Promise((r) => setTimeout(r, 0));
  const switched = [];
  const _realSwitch2 = window.switchReviewView;
  window.switchReviewView = (v) => switched.push(v);

  // enterReview is stubbed above, so drive the view classes directly.
  reviewSection.classList.add("view-board"); reviewSection.classList.remove("view-table");
  window.goToSubmittedView();
  checks.push(["board view: a stage jump filters the board instead of no-oping",
    window.getBoardFilter() === "applied" && switched.length === 0]);
  window.clearBoardFilter();

  reviewSection.classList.add("view-table"); reviewSection.classList.remove("view-board");
  window.goToSubmittedView();
  checks.push(["table view: a stage jump opens that stage's table",
    switched.length === 1 && switched[0] === "submitted" && window.getBoardFilter() === null]);

  // In table view the funnel goes back to being a tab bar.
  switched.length = 0;
  window.pipelineSegmentClick("failed");
  checks.push(["table view: a funnel click switches tab", switched.length === 1 && switched[0] === "failed"]);

  window.switchReviewView = _realSwitch2;
  window.enterReview = _realEnterReview;

  // --- counts: one store feeds the funnel AND the column badge ---
  reviewSection.classList.add("view-board"); reviewSection.classList.remove("view-table");
  window.setPipelineView("board");
  await window.renderBoard();
  window.setPipelineCount("applied", 7);
  checks.push(["a count update paints the column badge", doc.getElementById("kct-applied").textContent === "7"]);
  checks.push(["…and the funnel segment in the same write",
    doc.getElementById("pipeline-funnel").innerHTML.includes(">7<")]);
  checks.push(["a tab-named count normalises to its stage key", window.pipelineCount("applied") === 7]);

  // ===================================================================
  // 11. One drag = ONE request, however many times the board re-rendered.
  //     renderBoard() replaces host.innerHTML but the delegated listeners
  //     live on the host itself, so wiring them per render stacked a set
  //     per render — the 8s live refresh turned one drop into N POSTs
  //     (11 duplicate tailor runs, and 11 pending drafts, observed live).
  // ===================================================================
  window.setPipelineView("board");
  await window.renderBoard();
  await window.renderBoard();   // the live refresh re-renders the SAME host
  await window.renderBoard();
  await new Promise((r) => setTimeout(r, 0));

  const boardHost = doc.getElementById("pipeline-board");
  checks.push(["board DnD wiring is marked on the host so it happens once",
    boardHost.dataset.dndWired === "1"]);

  // Drop a Shortlisted card onto Tailoring — the transition that POSTs /tailor.
  // A successful drop re-renders the board, detaching these nodes, so every
  // step re-queries rather than holding a reference across a render.
  const tailorPosts = () => calls.filter((c) => c.url === "/api/jobs/job-1/tailor");
  const dropCol = () => doc.querySelector('.kcol[data-col="tailoring"]');
  const putCard = (id, cls = "kcard") => {
    const host = doc.getElementById("kcards-shortlisted");
    host.innerHTML = `<div class="${cls}" draggable="true" data-id="${id}" data-from="shortlisted"></div>`;
    return host.querySelector(".kcard");
  };
  const fire = (el, type) =>
    el.dispatchEvent(new window.Event(type, { bubbles: true, cancelable: true }));

  calls.length = 0;
  const dragCard = putCard("job-1");
  fire(dragCard, "dragstart");
  checks.push(["dragstart marks the card", dragCard.classList.contains("dragging")]);
  fire(dropCol(), "drop");
  await new Promise((r) => setTimeout(r, 0));
  checks.push(["three renders, one drop → exactly ONE tailor POST", tailorPosts().length === 1]);

  // Defence in depth: the drop handler claims the drag, so a stray second drop
  // (or a duplicate listener surviving a future refactor) no-ops.
  fire(dropCol(), "drop");
  await new Promise((r) => setTimeout(r, 0));
  checks.push(["a second drop without a new drag sends nothing", tailorPosts().length === 1]);

  // dragend still clears the visual state even though drop nulled the drag.
  putCard("job-2", "kcard dragging");
  boardHost.classList.add("drag-from-shortlisted");
  dropCol().classList.add("drop-active");
  fire(boardHost, "dragend");
  checks.push(["dragend clears the drag classes",
    !boardHost.querySelector(".dragging") && !boardHost.querySelector(".drop-active")
      && !boardHost.classList.contains("drag-from-shortlisted")]);

  // Re-wiring explicitly is a no-op too (renderBoard isn't the only caller).
  calls.length = 0;
  window._wireBoardDnD(boardHost);
  fire(putCard("job-1"), "dragstart");
  fire(dropCol(), "drop");
  await new Promise((r) => setTimeout(r, 0));
  checks.push(["an explicit re-wire adds no second listener", tailorPosts().length === 1]);

  const fail = report(checks);
  if (fail) {
    console.error(`\ntest_deck: ${fail} check(s) failed`);
    process.exit(1);
  }
  console.log("\ntest_deck.js: all checks passed");
  process.exit(0);
})();
