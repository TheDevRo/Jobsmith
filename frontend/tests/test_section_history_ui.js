// AI Edit modal: the Section picker (whole document or one section, sent to /revise),
// and the History list with "Undo last change" / "Go back to this" (restore), shown
// as text only. Same jsdom style as test_facts_ui.js; api() is stubbed.
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const ROOT = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost:8888/", virtualConsole: new VirtualConsole() });
const { window } = dom;
const doc = window.document;

const calls = [];
const toasts = [];
let revisions = [];
window.api = (url, opts = {}) => {
  const method = opts.method || "GET";
  calls.push([url, method, opts.body || null]);
  if (/\/sections\?/.test(url)) return Promise.resolve({ sections: [{ id: "SUMMARY", label: "SUMMARY" }, { id: "SKILLS", label: "SKILLS" }] });
  if (/\/revisions\?/.test(url)) return Promise.resolve({ revisions: revisions.map(r => ({ ...r })), cap: 30 });
  if (/\/restore$/.test(url)) {
    const id = Number(url.split("/").slice(-2)[0]);
    return Promise.resolve({ target: "resume", content: "RESTORED " + id, fact_flags: { resume: ["Globex"], cover_letter: [] } });
  }
  if (/\/revise$/.test(url)) return Promise.resolve({ revised_content: "NEW DOC", honesty_level: "honest", unverified: [], section: "SKILLS" });
  if (/\/facts/.test(url)) return Promise.resolve({ facts: [], max_facts: 50, max_chars: 1000 });
  return Promise.resolve({});
};
window.toast = (m, t) => toasts.push([m, t]);
window._aiEditDefaultTier = "strong"; window._aiEditDefaultHonesty = "honest";
window.eval(fs.readFileSync(path.join(ROOT, "js", "facts.js"), "utf8"));
window.eval(fs.readFileSync(path.join(ROOT, "js", "jobs-actions.js"), "utf8"));  // escapeHtml, safeParseJSON
window.eval(fs.readFileSync(path.join(ROOT, "js", "review.js"), "utf8"));

const tick = () => new Promise(r => setTimeout(r, 0));
const checks = [];

(async () => {
  revisions = [
    { id: 12, target: "resume", instruction: "make it <b>shorter</b>", created_at: "2026-01-02T10:00:00+00:00", preview: "R2" },
    { id: 11, target: "resume", instruction: null, created_at: "2026-01-01T10:00:00+00:00", preview: "R1" },
  ];
  window._reviewApps = { app1: { resume_content: "R", cover_letter_content: "C" } };
  window.openAiEditModal("app1", "resume");
  await tick(); await tick();
  const modal = doc.getElementById("ai-edit-modal");
  const sel = modal.querySelector("#ai-edit-section");

  checks.push(["section picker offers Whole document first, then each section",
    [...sel.options].map(o => o.value).join(",") === ",SUMMARY,SKILLS" && sel.options[0].textContent === "Whole document"]);
  checks.push(["sections are read for the current target", calls.some(c => c[0] === "/api/applications/app1/sections?target=resume")]);

  const body = modal.querySelector("#ai-edit-history-body");
  const buttons = [...body.querySelectorAll("button")];
  checks.push(["history lists newest first with Undo last change + Go back to this",
    buttons.map(b => b.textContent).join("|") === "Undo last change|Go back to this|Go back to this"]);
  checks.push(["instruction shown as text, blank as Manual edit; never parsed as HTML",
    body.textContent.includes("make it <b>shorter</b>") && body.textContent.includes("Manual edit") && body.querySelector("b") === null]);

  // Section is sent with the revision request; whole document sends none.
  modal.querySelector("#ai-edit-instructions").value = "add Go";
  sel.value = "SKILLS";
  await window.runAiRevision();
  const sent = JSON.parse(calls.filter(c => c[0].endsWith("/revise")).pop()[2]);
  checks.push(["selected section is sent to /revise", sent.section === "SKILLS" && sent.target === "resume"]);
  checks.push(["a revision result collapses Facts and History so the panes get the room",
    modal.querySelector("#ai-edit-facts-box").open === false && modal.querySelector("#ai-edit-history").open === false]);
  sel.value = "";
  await window.runAiRevision();
  checks.push(["whole document sends no section", !("section" in JSON.parse(calls.filter(c => c[0].endsWith("/revise")).pop()[2]))]);

  // Accept sends the instruction so it's kept with the version it replaces.
  calls.length = 0;
  await window.acceptAiRevision();
  const patch = calls.find(c => c[1] === "PATCH" && c[0].endsWith("/content"));
  checks.push(["accept sends the instruction", patch && JSON.parse(patch[2]).instruction === "add Go"]);

  // Undo last change restores the newest revision and updates the screen.
  window.openAiEditModal("app1", "resume");
  await tick(); await tick();
  calls.length = 0;
  const m2 = doc.getElementById("ai-edit-modal");
  const hostCheck = doc.createElement("div"); hostCheck.id = "factcheck-app1"; doc.body.appendChild(hostCheck);
  m2.querySelector("#ai-edit-history-body button").click();
  await tick(); await tick();
  checks.push(["Undo last change restores the newest revision", calls.some(c => c[0] === "/api/applications/app1/revisions/12/restore" && c[1] === "POST")]);
  checks.push(["restored text is now the document and the history reloads",
    window._reviewApps.app1.resume_content === "RESTORED 12" && calls.some(c => /revisions\?target=resume/.test(c[0]))]);
  checks.push(["restore refreshes the Check this notice", hostCheck.textContent === "Check this: Globex"]);

  // Go back to this restores that specific revision.
  calls.length = 0;
  [...doc.querySelectorAll("#ai-edit-history-body button")].filter(b => b.textContent === "Go back to this")[1].click();
  await tick(); await tick();
  checks.push(["Go back to this restores that revision", calls.some(c => c[0].endsWith("/revisions/11/restore"))]);

  // After a restore, the newest entry is the restore itself: the button offers Redo.
  revisions = [{ id: 13, target: "resume", instruction: "Restored an earlier version", created_at: "2026-01-03T10:00:00+00:00", preview: "R3" }, ...revisions];
  await window._aiEditLoadHistory();
  checks.push(["after a restore the first button reads Redo", doc.querySelector("#ai-edit-history-body button").textContent === "Redo"]);

  revisions = [];
  await window._aiEditLoadHistory();
  checks.push(["empty history says so and offers no buttons",
    doc.querySelector("#ai-edit-history-body").textContent.includes("No earlier versions") && !doc.querySelector("#ai-edit-history-body button")]);

  let fail = 0;
  for (const [name, ok] of checks) { console.log((ok ? "PASS" : "FAIL") + "  " + name); if (!ok) fail++; }
  if (fail) { console.error(`\ntest_section_history_ui.js: ${fail} check(s) failed`); process.exit(1); }
  console.log("\ntest_section_history_ui.js: all checks passed");
  process.exit(0);
})();
