// Phase 0 structural test: the job actions registry (frontend/js/job-actions.js)
// is the ONE place that decides which per-job actions a surface offers.
//
// What this pins down:
//   a) parity — the same job produces the same action ids in every context,
//      modulo that context's declared whitelist (no context may invent an
//      action, or silently drop one it lists, on its own);
//   b) the unified Apply Assist rule — offered iff the posting has a real URL
//      and isn't deleted, identically everywhere, with no disabled state;
//   c) the recycle-bin set — a deleted job offers Restore + Erase (+ Open Job
//      URL where the context lists it) and nothing else;
//   d) a golden action list + golden markup for a normal external-apply job.
//
// Same jsdom-as-one-eval-unit style as test_deck.js / test_checklist.js.
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
const dom = new JSDOM(`<!DOCTYPE html><html><body></body></html>`, {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  url: "http://localhost:8888/",
  virtualConsole,
});
const { window } = dom;

// job-actions.js leans on escapeHtml/safeId/safeHref, which live in
// jobs-actions.js — load the pair, nothing else is needed.
const SCRIPTS = ["jobs-actions.js", "job-actions.js"];
window.eval(SCRIPTS.map((f) => fs.readFileSync(path.join(JS_DIR, f), "utf8")).join("\n;\n"));

const { jobActions, jobActionIds, renderJobActions, jobActionWhitelist: whitelist } = window;
const CONTEXTS = ["list-row", "detail", "peek", "kanban-menu", "review-row", "review-detail"];

const checks = [];

// ==========================================================================
// (a) Parity: ids in a context are exactly that context's whitelist filtered
//     by the shared visibility rules — never more, never differently ordered.
// ==========================================================================
const jobs = {
  "external w/ url + app": {
    id: "j1", title: "T", company: "C", source: "s", status: "shortlisted",
    url: "https://jobs.test/1", apply_type: "external", fit_score: 82, app_id: "a1",
  },
  "plain, no url, unscored": { id: "j2", title: "T", source: "s", status: "discovered" },
  "already applied": {
    id: "j3", title: "T", source: "s", status: "applied", app_status: "applied",
    url: "https://jobs.test/3",
  },
  deleted: { id: "j4", title: "T", source: "s", status: "deleted", app_status: "pending_review", url: "https://jobs.test/4" },
};

// One shared visibility verdict per (job, action) — computed once from any
// context that offers it, then asserted to hold in every other context that
// lists it. This is the anti-drift assertion.
for (const [label, job] of Object.entries(jobs)) {
  const state = job.status === "deleted" ? "deleted" : "live";
  const verdicts = {};        // action id → shown? (first context wins)
  let consistent = true;
  let subsetOfWhitelist = true;
  let orderedLikeWhitelist = true;

  for (const ctx of CONTEXTS) {
    const wl = whitelist(ctx, state);
    const ids = jobActionIds(job, ctx);
    if (!ids.every((id) => wl.includes(id))) subsetOfWhitelist = false;
    // Filtering must preserve the whitelist's order.
    if (ids.join(",") !== wl.filter((id) => ids.includes(id)).join(",")) orderedLikeWhitelist = false;
    for (const id of wl) {
      const shown = ids.includes(id);
      if (verdicts[id] === undefined) verdicts[id] = shown;
      else if (verdicts[id] !== shown) consistent = false;
    }
  }
  checks.push([`[${label}] every context's ids come from its whitelist`, subsetOfWhitelist]);
  checks.push([`[${label}] filtering preserves whitelist order`, orderedLikeWhitelist]);
  checks.push([`[${label}] no context disagrees about an action's visibility`, consistent]);
}

// Sanity: the whitelists really do differ, so the parity check above is not
// vacuously true.
checks.push([
  "contexts have genuinely different whitelists",
  whitelist("list-row", "live").join() !== whitelist("detail", "live").join()
    && whitelist("review-row", "live").join() !== whitelist("detail", "live").join(),
]);
// peek is the detail context by construction — the old modal-only "always
// assist" override must not come back.
checks.push([
  "peek renders exactly the detail actions",
  jobActionIds(jobs["external w/ url + app"], "peek").join() ===
    jobActionIds(jobs["external w/ url + app"], "detail").join(),
]);

// ==========================================================================
// (b) Unified Apply Assist: URL present and not deleted — nothing else.
// ==========================================================================
const assistCtxs = ["list-row", "detail", "peek", "review-detail"];
const withUrl = { id: "u1", url: "https://jobs.test/u", status: "shortlisted" };
const withUrlExternal = { id: "u2", url: "https://jobs.test/u", status: "shortlisted", apply_type: "external" };
const noUrl = { id: "u3", status: "shortlisted" };
const badUrl = { id: "u4", status: "shortlisted", url: "javascript:alert(1)" };
const deletedWithUrl = { id: "u5", status: "deleted", url: "https://jobs.test/u" };

checks.push(["Apply Assist shows for a URL job in every assist context",
  assistCtxs.every((c) => jobActionIds(withUrl, c).includes("assist"))]);
checks.push(["apply_type never changes the Apply Assist verdict",
  assistCtxs.every((c) => jobActionIds(withUrl, c).includes("assist") === jobActionIds(withUrlExternal, c).includes("assist"))]);
checks.push(["no URL → no Apply Assist in any context",
  CONTEXTS.every((c) => !jobActionIds(noUrl, c).includes("assist"))]);
checks.push(["a non-http URL is not a URL (safeHref) → no Apply Assist",
  CONTEXTS.every((c) => !jobActionIds(badUrl, c).includes("assist"))]);
checks.push(["deleted job never offers Apply Assist even with a URL",
  CONTEXTS.every((c) => !jobActionIds(deletedWithUrl, c).includes("assist"))]);
checks.push(["Apply Assist is never rendered disabled",
  assistCtxs.every((c) => !renderJobActions(withUrl, c).includes("disabled"))]);
// Contexts that never offered it still don't (whitelist intent, not drift).
checks.push(["review-row and the kanban menu still offer no Apply Assist",
  !jobActionIds(withUrl, "review-row").includes("assist") && !jobActionIds(withUrl, "kanban-menu").includes("assist")]);

// ==========================================================================
// (c) Recycle bin: Restore + Erase only, and only in the deleted contexts.
// ==========================================================================
const del = jobs.deleted;
checks.push(["deleted list row = Restore, Erase", jobActionIds(del, "list-row").join() === "restore,erase"]);
checks.push(["deleted detail = Restore, Open Job URL, Erase",
  jobActionIds(del, "detail").join() === "restore,open-url,erase"]);
checks.push(["deleted peek matches deleted detail",
  jobActionIds(del, "peek").join() === jobActionIds(del, "detail").join()]);
checks.push(["a deleted job offers nothing in the kanban menu / Pipeline",
  ["kanban-menu", "review-row", "review-detail"].every((c) => jobActionIds(del, c).length === 0)]);
checks.push(["a deleted job never offers Delete / Tailor / Mark Applied",
  CONTEXTS.every((c) => {
    const ids = jobActionIds(del, c);
    return !ids.includes("delete") && !ids.includes("tailor") && !ids.includes("mark-applied");
  })]);
// Erase/Restore are Recently-Deleted-only: they must never leak into a live
// job in any context, and no live whitelist may even list them.
checks.push(["Erase/Restore never appear for a live job",
  Object.values(jobs).filter((j) => j.status !== "deleted").every((j) =>
    CONTEXTS.every((c) => {
      const ids = jobActionIds(j, c);
      return !ids.includes("erase") && !ids.includes("restore");
    }))]);
checks.push(["no live whitelist even lists Erase/Restore",
  CONTEXTS.every((c) => !whitelist(c, "live").includes("erase") && !whitelist(c, "live").includes("restore"))]);
checks.push(["deleted-detail markup keeps the recycle-bin copy",
  (() => {
    const html = renderJobActions(del, "detail");
    return html.includes(">Restore to Inbox<") && html.includes(">Erase Permanently<") && html.includes("eraseJob('j4')");
  })()]);
checks.push(["deleted-row markup uses the short row copy",
  (() => {
    const html = renderJobActions(del, "list-row");
    return html.includes(">Restore<") && html.includes(">Erase<") && html.includes("btn-xs")
      && html.includes("event.stopPropagation();restoreJob(");
  })()]);

// ==========================================================================
// (d) Golden output for a normal external-apply job.
// ==========================================================================
const ext = jobs["external w/ url + app"];
checks.push(["golden: detail ids",
  jobActionIds(ext, "detail").join() ===
    "score,tailor,assist,view-application,mark-applied,embellishments,delete"]);
checks.push(["golden: external apply_type still suppresses Open Job URL",
  !jobActionIds(ext, "detail").includes("open-url")
    && jobActionIds({ ...ext, apply_type: undefined }, "detail").includes("open-url")]);
checks.push(["golden: list row ids", jobActionIds(ext, "list-row").join() === "assist"]);
checks.push(["golden: review row ids", jobActionIds(ext, "review-row").join() === "tailor,score,pass"]);
checks.push(["golden: review detail ids", jobActionIds(ext, "review-detail").join() === "assist"]);
checks.push(["golden: kanban menu ids", jobActionIds(ext, "kanban-menu").join() === "delete"]);

const detailHtml = renderJobActions(ext, "detail");
for (const [name, needle] of [
  ["Rescore (job is scored)", `<button class="btn btn-secondary btn-sm" onclick="scoreJob('j1')">Rescore</button>`],
  ["Tailor Resume + its title", `<button class="btn btn-primary btn-sm" onclick="tailorJob('j1')" title="Generate a resume and cover letter customized to this job">Tailor Resume</button>`],
  ["Apply Assist", `<button class="btn btn-assist btn-sm" onclick="launchAssist('j1')">Apply Assist</button>`],
  ["View Application (app_id present)", `<button class="btn btn-secondary btn-sm" onclick="viewApplicationFor('j1')">View Application</button>`],
  ["Mark Applied", `<button class="btn btn-green btn-sm" onclick="markApplied('j1')">Mark Applied</button>`],
  ["Embellishments", `<button class="btn btn-secondary btn-sm" onclick="toggleEmbPanel('j1')">Embellishments</button>`],
  ["Delete", `<button class="btn btn-danger btn-sm" onclick="deleteSingleJob('j1')">Delete</button>`],
]) {
  checks.push([`golden markup: ${name}`, detailHtml.includes(needle)]);
}
checks.push(["golden markup: Score label when unscored",
  renderJobActions({ id: "j9", status: "shortlisted" }, "detail").includes(">Score<")]);
checks.push(["golden markup: applied job drops Mark Applied",
  !jobActionIds(jobs["already applied"], "detail").includes("mark-applied")]);
checks.push(["golden markup: no app_id → no View Application",
  !jobActionIds(jobs["plain, no url, unscored"], "detail").includes("view-application")]);
checks.push(["golden markup: Open Job URL keeps the open-url hooks",
  (() => {
    const html = renderJobActions({ id: "j8", status: "shortlisted", url: "https://jobs.test/8" }, "detail");
    return html.includes(`<a class="btn btn-secondary btn-sm" href="https://jobs.test/8" target="_blank" rel="noopener" data-jobsmith-open-url data-jobsmith-job-id="j8">Open Job URL</a>`);
  })()]);
checks.push(["golden markup: kanban menu item keeps its menuitem shape",
  renderJobActions({ id: "j1" }, "kanban-menu") ===
    `<button role="menuitem" class="kmenu-danger" onclick="_runCardMenuDelete('j1')">Delete posting</button>`]);
checks.push(["golden markup: review row keeps btn-xs + ghost Pass",
  (() => {
    const html = renderJobActions(ext, "review-row");
    return html.includes(`<button class="btn btn-primary btn-xs" onclick="tailorJob('j1')" title="Generate a resume and cover letter customized to this job">Tailor</button>`)
      && html.includes(`<button class="btn btn-ghost btn-xs" onclick="passShortlisted('j1')">Pass</button>`);
  })()]);

// Hostile ids never break out of the inline handler (safeId, not escapeHtml).
checks.push(["hostile job ids are stripped from inline handlers",
  (() => {
    const html = renderJobActions({ id: "x');alert(1);('", status: "shortlisted" }, "detail");
    // safeId (not escapeHtml) guards inline handlers: the HTML parser decodes
    // entities before the JS string is parsed, so an escaped quote would still
    // break out. Nothing executable survives in any onclick.
    const handlers = html.match(/onclick="[^"]*"/g) || [];
    return handlers.length >= 5
      // No quote, paren or semicolon from the id survives, so nothing can
      // close the JS string literal — "xalert1" is inert text, not a call.
      && handlers.every((h) => !h.includes("alert(") && !h.includes(";") && !h.includes("&#39;"))
      && html.includes("scoreJob('xalert1')")
      // The data attribute is a plain attribute value — escapeHtml is correct
      // and sufficient there.
      && html.includes(`data-jobsmith-job-id="x&#39;);alert(1);(&#39;"`);
  })()]);

// Unknown context and missing job degrade to nothing, never to a throw.
checks.push(["unknown context yields no actions", jobActions(ext, "nope").length === 0]);
checks.push(["null job yields no actions", jobActions(null, "detail").length === 0 && renderJobActions(null, "detail") === ""]);

// Descriptor shape is stable for consumers that want more than `html`.
checks.push(["descriptors carry the documented shape",
  jobActions(ext, "detail").every((a) =>
    typeof a.id === "string" && typeof a.label === "string" && typeof a.title === "string"
    && ["primary", "secondary", "danger", "assist", "green", "ghost"].includes(a.kind)
    && typeof a.onclickAttr === "string" && typeof a.html === "string" && a.visible === true)]);

const fail = report(checks);
if (fail) {
  console.error(`\ntest_job_actions: ${fail} check(s) failed`);
  process.exit(1);
}
console.log("\ntest_job_actions.js: all checks passed");
