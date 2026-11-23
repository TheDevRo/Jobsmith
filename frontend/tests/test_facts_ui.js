// Facts editor: the shared list widget behind "Facts about me" (Settings) and
// "Facts for this application" (AI Edit modal). Add / edit / remove call the API
// and re-read the list, an API error shows a toast and keeps the list, and the AI
// Edit modal mounts the editor above the instructions and can keep an instruction
// as a fact. Same jsdom style as test_nli_beta.js; api() is stubbed.
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const ROOT = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost:8888/", virtualConsole: new VirtualConsole() });
const { window } = dom;
const doc = window.document;

let store = [];
let nextId = 1;
let failNext = null;
const calls = [];
const toasts = [];
window.api = (url, opts = {}) => {
  const method = opts.method || "GET";
  calls.push([url, method, opts.body || null]);
  if (failNext) { const m = failNext; failNext = null; return Promise.reject(new Error(m)); }
  const body = opts.body ? JSON.parse(opts.body) : {};
  if (/\/revise$/.test(url)) return Promise.resolve({ revised_content: "Served 90,000 users at Globex.", honesty_level: "tailored", unverified: ["Globex", "90,000"] });
  if (/\/content$/.test(url)) return Promise.resolve({});
  if (/\/facts$/.test(url) && method === "GET") return Promise.resolve({ facts: store.map(f => ({ ...f })), max_facts: 50, max_chars: 1000 });
  if (/\/facts$/.test(url) && method === "POST") { store.push({ id: nextId++, text: body.text }); return Promise.resolve({}); }
  const id = Number(url.split("/").pop());
  if (method === "PATCH") { store.find(f => f.id === id).text = body.text; return Promise.resolve({}); }
  if (method === "DELETE") { store = store.filter(f => f.id !== id); return Promise.resolve({ ok: true }); }
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
  const host = doc.createElement("div");
  doc.body.appendChild(host);
  const ed = window.mountFactsEditor(host, { path: "/api/profile/facts", label: "Facts about me", hint: "used next time", placeholder: "x" });
  await tick();
  checks.push(["empty list shows the label with the 0/50 count and the hint",
    host.textContent.includes("Facts about me (0/50)") && host.textContent.includes("used next time")]);

  const input = host.querySelector("input");
  input.value = "  I led a team of five  ";
  [...host.querySelectorAll("button")].find(b => b.textContent === "Add").click();
  await tick(); await tick();
  checks.push(["Add POSTs the text and the list re-renders", calls.some(c => c[1] === "POST" && c[2].includes("led a team")) &&
    host.querySelectorAll(".fact-row").length === 1 && host.textContent.includes("(1/50)")]);

  host.querySelector(".fact-row button").click(); // Edit
  const ta = host.querySelector("textarea");
  ta.value = "Edited fact";
  [...host.querySelectorAll("button")].find(b => b.textContent === "Save").click();
  await tick(); await tick();
  checks.push(["Edit PATCHes the fact", calls.some(c => c[1] === "PATCH") && host.textContent.includes("Edited fact")]);

  failNext = "A fact must be at most 1000 characters";
  host.querySelector("input").value = "too long";
  [...host.querySelectorAll("button")].find(b => b.textContent === "Add").click();
  await tick(); await tick();
  checks.push(["an API error is toasted and the list is kept",
    toasts.some(t => t[0].includes("at most 1000") && t[1] === "error") && host.querySelectorAll(".fact-row").length === 1]);

  [...host.querySelectorAll(".fact-row button")].find(b => b.textContent === "Remove").click();
  await tick(); await tick();
  checks.push(["Remove DELETEs the fact", calls.some(c => c[1] === "DELETE") && host.querySelectorAll(".fact-row").length === 0]);

  // Fact text is shown as text, never parsed as HTML.
  store = [{ id: 99, text: "<img src=x onerror=alert(1)>" }];
  await ed.reload();
  checks.push(["fact text is not parsed as HTML", host.querySelector("img") === null && host.textContent.includes("<img")]);

  // AI Edit modal: facts list above the instructions + "Keep this for future edits".
  store = []; calls.length = 0;
  window._reviewApps = { app1: { resume_content: "R", cover_letter_content: "C" } };
  window.openAiEditModal("app1", "resume");
  await tick();
  const modal = doc.getElementById("ai-edit-modal");
  const factsBox = modal.querySelector("#ai-edit-facts");
  const instr = modal.querySelector("#ai-edit-instructions");
  checks.push(["modal loads this application's facts", calls.some(c => c[0] === "/api/applications/app1/facts" && c[1] === "GET")]);
  checks.push(["facts list sits above the instructions box",
    factsBox.textContent.includes("Facts for this application") &&
    (factsBox.compareDocumentPosition(instr) & window.Node.DOCUMENT_POSITION_FOLLOWING) !== 0]);
  checks.push(["hint says when facts are used", /next time the AI writes or rewrites/.test(factsBox.textContent)]);

  instr.value = "Emphasise my Python work";
  // jsdom (outside-only) doesn't run inline onclick; check the wiring, then call it.
  checks.push(["the button is wired to pinAiEditInstruction", modal.querySelector("#ai-edit-pin-btn").getAttribute("onclick") === "pinAiEditInstruction()"]);
  await window.pinAiEditInstruction();
  await tick(); await tick();
  checks.push(["Keep this for future edits saves the instruction as a fact",
    calls.some(c => c[1] === "POST" && c[2].includes("Emphasise my Python work")) && factsBox.textContent.includes("Emphasise my Python work")]);

  // "Check this" notice: shown in the AI Edit result pane, never blocks Accept & Save.
  const fcBox = modal.querySelector("#ai-edit-factcheck");
  checks.push(["no notice before a revision", fcBox.textContent === ""]);
  instr.value = "Make it punchier";
  await window.runAiRevision();
  checks.push(["result pane lists the flagged terms", fcBox.textContent === "Check this: Globex, 90,000"]);
  checks.push(["Accept & Save stays enabled (warning only)", modal.querySelector("#ai-edit-accept-btn").disabled === false]);
  checks.push(["terms are escaped", window.renderFactCheck(["<b>x</b>"]).includes("&lt;b&gt;") && window.renderFactCheck([]) === ""]);

  // Accepting sends the level used and refreshes the review card's notice.
  const reviewHost = doc.createElement("div");
  reviewHost.id = "factcheck-app1";
  doc.body.appendChild(reviewHost);
  calls.length = 0;
  await window.acceptAiRevision();
  const patch = calls.find(c => c[1] === "PATCH" && c[0].endsWith("/content"));
  checks.push(["accept sends the honesty level to the content PATCH", patch && JSON.parse(patch[2]).honesty_level === "tailored"]);
  checks.push(["review screen shows the same flags after saving", reviewHost.textContent === "Check this: Globex, 90,000"]);

  let fail = 0;
  for (const [name, ok] of checks) { console.log((ok ? "PASS" : "FAIL") + "  " + name); if (!ok) fail++; }
  if (fail) { console.error(`\ntest_facts_ui.js: ${fail} check(s) failed`); process.exit(1); }
  console.log("\ntest_facts_ui.js: all checks passed");
  process.exit(0);
})();
