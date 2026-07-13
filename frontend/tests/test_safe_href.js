// SEC-03 regression test: scraped job URLs must never render as a live
// `javascript:` (or other non-http) href.
//
// Job `url` values come from external job boards and are attacker-controlled.
// `escapeHtml` neutralizes <>&"' but NOT the URL scheme, so before this fix a
// job with url:"javascript:alert(1)" rendered a working XSS link in the
// dashboard's own origin. `safeHref()` collapses anything that isn't a plain
// http(s) URL to '#'.
//
// Mirrors the conventions in extension/tests/: jsdom + the production scripts
// eval'd unmodified, a [name, bool] checks array, and report().
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const JS_DIR = path.join(__dirname, "..", "js");

function report(checks) {
  let fail = 0;
  for (const [name, ok] of checks) {
    console.log((ok ? "PASS" : "FAIL") + "  " + name);
    if (!ok) fail++;
  }
  return fail;
}

// Minimal DOM: just the nodes the render paths touch.
const dom = new JSDOM(
  `<!DOCTYPE html><html><body>
     <div id="app-banners"></div>
     <div id="toast-container"></div>
     <div id="jobs-list"></div>
     <div id="jobs-pagination"></div>
     <input type="checkbox" id="select-all-jobs">
     <span id="selected-count"></span>
     <div id="job-detail-pane"></div>
     <div id="theme-toggle"></div>
   </body></html>`,
  // runScripts: "dangerously" is required, not incidental: this frontend wires
  // everything through inline onclick/onkeydown attributes, and jsdom only
  // executes those under "dangerously". Under "outside-only" the keyboard check
  // below would pass vacuously. Nothing untrusted is parsed here.
  //
  // A real origin is also required: core.js's initTheme touches localStorage,
  // which throws a SecurityError on jsdom's default opaque (about:blank)
  // origin. Port 8888 keeps the EOU-03 port banner out of the way.
  { runScripts: "dangerously", pretendToBeVisual: true, url: "http://localhost:8888/" }
);
const { window } = dom;
const doc = window.document;

// The frontend is classic scripts sharing ONE global scope. They're
// concatenated and eval'd as a single unit rather than one eval each, because
// top-level `let`/`const` in a classic script becomes a global lexical binding
// visible to every later script (e.g. jobs.js reads jobs-actions.js's
// `selectModeActive`), whereas separate evals would scope them per-eval.
// Order matches index.html.
const SCRIPTS = ["core.js", "jobs.js", "jobs-actions.js"];
window.eval(
  SCRIPTS.map((f) => fs.readFileSync(path.join(JS_DIR, f), "utf8")).join("\n;\n")
);

const checks = [];

// ---- safeHref() unit behavior ----
const safeHref = window.safeHref;
checks.push(["javascript: URL collapses to #", safeHref("javascript:alert(1)") === "#"]);
checks.push(["mixed-case JaVaScRiPt: collapses to #", safeHref("JaVaScRiPt:alert(1)") === "#"]);
checks.push(["leading-whitespace javascript: collapses to #", safeHref("  javascript:alert(1)") === "#"]);
checks.push(["data: URL collapses to #", safeHref("data:text/html,<script>alert(1)</script>") === "#"]);
checks.push(["file: URL collapses to #", safeHref("file:///etc/passwd") === "#"]);
checks.push(["protocol-relative URL collapses to #", safeHref("//evil.example/x") === "#"]);
checks.push(["null/undefined collapses to #", safeHref(null) === "#" && safeHref(undefined) === "#"]);
checks.push(["https URL passes through", safeHref("https://example.com/job/1") === "https://example.com/job/1"]);
checks.push(["http URL passes through", safeHref("http://example.com/job/1") === "http://example.com/job/1"]);

// ---- The real render path: a malicious job rendered into the detail pane ----
const evilJob = {
  id: "evil-1",
  title: "Senior Engineer",
  company: "Evil Corp",
  location: "Remote",
  source: "linkedin",
  status: "discovered",
  url: "javascript:alert(1)",
  apply_type: "easy_apply", // anything but 'external' renders the Open Job URL link
  description: "hi",
};

window.renderJobs([evilJob], 1);
window.selectJob("evil-1");

const anchors = Array.from(doc.querySelectorAll("#job-detail-pane a"));
const jobUrlAnchor = anchors.find((a) => a.textContent.includes("Open Job URL"));

checks.push(["Open Job URL anchor renders", !!jobUrlAnchor]);
checks.push([
  'javascript: job url renders href="#"',
  !!jobUrlAnchor && jobUrlAnchor.getAttribute("href") === "#",
]);
checks.push([
  "no anchor anywhere has a javascript: href",
  !Array.from(doc.querySelectorAll("a[href]")).some((a) =>
    /^\s*javascript:/i.test(a.getAttribute("href") || "")
  ),
]);

// A benign job still gets a working link (the guard isn't over-broad).
const goodJob = { ...evilJob, id: "good-1", url: "https://example.com/job/1" };
window.renderJobs([goodJob], 1);
window.selectJob("good-1");
const goodAnchor = Array.from(doc.querySelectorAll("#job-detail-pane a")).find((a) =>
  a.textContent.includes("Open Job URL")
);
checks.push([
  "https job url renders the real href",
  !!goodAnchor && goodAnchor.getAttribute("href") === "https://example.com/job/1",
]);

// ---- Defense in depth: the click guard blocks unsafe schemes ----
const planted = doc.createElement("a");
planted.setAttribute("href", "javascript:alert(1)");
planted.textContent = "planted";
doc.body.appendChild(planted);
const ev = new window.MouseEvent("click", { bubbles: true, cancelable: true });
planted.dispatchEvent(ev);
checks.push(["click guard blocks a javascript: anchor", ev.defaultPrevented === true]);

// ---- Keyboard accessibility (UX-04) ----
window.renderJobs([goodJob], 1);
const card = doc.querySelector(".job-card");
checks.push([
  "job card is keyboard-focusable",
  !!card && card.getAttribute("role") === "button" && card.getAttribute("tabindex") === "0",
]);
// Clear the pane first, so a repopulated pane can only have come from the
// keydown handler (and not from the selectJob() calls above).
doc.getElementById("job-detail-pane").innerHTML = "";
const key = new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
card.dispatchEvent(key);
checks.push([
  "Enter on a job card selects it",
  !!doc.querySelector("#job-detail-pane .detail-title"),
]);
checks.push(["Enter is preventDefault'd (no page scroll)", key.defaultPrevented === true]);

doc.getElementById("job-detail-pane").innerHTML = "";
const spaceKey = new window.KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true });
card.dispatchEvent(spaceKey);
checks.push([
  "Space on a job card selects it",
  !!doc.querySelector("#job-detail-pane .detail-title"),
]);

// An unrelated key must not hijack the card.
doc.getElementById("job-detail-pane").innerHTML = "";
card.dispatchEvent(new window.KeyboardEvent("keydown", { key: "a", bubbles: true, cancelable: true }));
checks.push([
  "other keys do not select the card",
  !doc.querySelector("#job-detail-pane .detail-title"),
]);

const fail = report(checks);
if (fail) {
  console.error(`\nsafeHref: ${fail} check(s) failed`);
  process.exit(1);
}
console.log("\nsafe_href.js: all checks passed");
process.exit(0);
