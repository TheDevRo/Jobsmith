// Functional test of src/common/dropcatch.js (drag-to-attach interception).
const { loadDom, evalScript, report } = require("./helpers");

const html = `<!DOCTYPE html><html><body>
  <form>
    <div class="dropzone" style="position:absolute">
      <input type="file" id="resume-input">
    </div>
    <div>
      <input type="file" id="cover-input">
    </div>
    <button id="page-btn">Submit</button>
  </form>
</body></html>`;

const dom = loadDom(html);
const { window } = dom;
const doc = window.document;

// Pull-based: drops are recorded in the page and consumed via
// __jobsmithTakeDropResult (runtime messaging is unreliable into extension
// iframes in Firefox).
const take = () => window.__jobsmithTakeDropResult();
const messages = [];  // filled by takeAll() below for assertion convenience
function takeAll() { const r = take(); if (r) messages.push(r); return r; }

// Distinct rects so nearest-input selection is decidable: resume dropzone on
// the left, cover input on the right.
const rects = new Map([
  ["resume-input", { left: 0, right: 200, top: 0, bottom: 100 }],
  ["cover-input", { left: 600, right: 800, top: 0, bottom: 100 }],
]);
window.HTMLElement.prototype.getBoundingClientRect = function () {
  for (const [id, r] of rects) {
    const inp = doc.getElementById(id);
    if (inp && (this === inp || this.contains(inp))) {
      return { ...r, width: r.right - r.left, height: r.bottom - r.top };
    }
  }
  return { left: 0, right: 10, top: 0, bottom: 10, width: 10, height: 10 };
};

evalScript(window, "common/dropcatch.js");

const checks = [];

// Armed: dropzones highlighted
window.__jobsmithArmDropCatch("resume");
checks.push(["arming stamps dropzones", doc.querySelectorAll('[data-jobsmith-dropzone="1"]').length === 2]);
checks.push(["highlight style injected", !!doc.getElementById("__jobsmith-dropcatch-style__")]);

// Page's own drop handler must never fire while we intercept
let pageSawDrop = false;
doc.addEventListener("drop", () => { pageSawDrop = true; });

// Drop near the left dropzone → resume input picked, message sent
const drop = new window.MouseEvent("drop", { bubbles: true, cancelable: true, clientX: 50, clientY: 50 });
doc.getElementById("page-btn").dispatchEvent(drop);

const m = takeAll();
checks.push(["drop intercepted before page handler", !pageSawDrop]);
checks.push(["result recorded with ok+kind", m && m.ok === true && m.kind === "resume"]);
checks.push(["result consumed exactly once", take() === null]);
checks.push(["nearest input stamped with fid", m && doc.getElementById("resume-input").getAttribute("data-jobsmith-fid") === m.fid]);
checks.push(["auto-disarmed after drop", !doc.getElementById("__jobsmith-dropcatch-style__")]);

// Drop on the right side picks the cover input
window.__jobsmithArmDropCatch("cover_letter");
doc.getElementById("page-btn").dispatchEvent(
  new window.MouseEvent("drop", { bubbles: true, cancelable: true, clientX: 700, clientY: 50 })
);
const m2 = takeAll();
checks.push(["right-side drop picks cover input", m2 && m2.ok && doc.getElementById("cover-input").getAttribute("data-jobsmith-fid") === m2.fid]);

// Keyword dominance: dragging a RESUME but dropping over the cover zone must
// still pick the resume input (its id says "resume", the other says "cover").
window.__jobsmithArmDropCatch("resume");
doc.getElementById("page-btn").dispatchEvent(
  new window.MouseEvent("drop", { bubbles: true, cancelable: true, clientX: 700, clientY: 50 })
);
const m3 = takeAll();
checks.push(["resume drag over cover zone still picks resume input", m3 && m3.ok && doc.getElementById("resume-input").getAttribute("data-jobsmith-fid") === m3.fid]);

// Manual disarm removes listeners: a later drop does nothing
window.__jobsmithArmDropCatch("resume");
window.__jobsmithDisarmDropCatch();
doc.getElementById("page-btn").dispatchEvent(
  new window.MouseEvent("drop", { bubbles: true, cancelable: true, clientX: 50, clientY: 50 })
);
checks.push(["disarm stops interception", take() === null]);

const fail = report(checks);
if (fail) { console.log("messages:", JSON.stringify(messages)); process.exit(1); }
console.log("\ndropcatch.js: all checks passed");
