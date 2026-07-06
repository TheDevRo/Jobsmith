// helpers.js — shared jsdom setup for extension tests.
//
// jsdom differs from a real browser in two ways that matter here:
//   - CSS.escape is missing (content scripts always have it)
//   - getBoundingClientRect always returns zeros (snapshot/fill treat 0x0
//     elements as invisible)
// Both are polyfilled so the production scripts run unmodified.

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const SRC_DIR = path.join(__dirname, "..", "src");

function loadDom(html) {
  const dom = new JSDOM(html, { runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  if (!window.CSS) window.CSS = {};
  if (!window.CSS.escape) {
    window.CSS.escape = (s) => String(s).replace(/([^a-zA-Z0-9_-])/g, "\\$1");
  }
  window.HTMLElement.prototype.getBoundingClientRect = function () {
    return { width: 100, height: 20, top: 0, left: 0, right: 100, bottom: 20 };
  };
  return dom;
}

function evalScript(window, relPath) {
  const src = fs.readFileSync(path.join(SRC_DIR, relPath), "utf8");
  return window.eval(src);
}

function report(checks) {
  let fail = 0;
  for (const [name, ok] of checks) {
    console.log((ok ? "PASS" : "FAIL") + "  " + name);
    if (!ok) fail++;
  }
  return fail;
}

module.exports = { loadDom, evalScript, report };
