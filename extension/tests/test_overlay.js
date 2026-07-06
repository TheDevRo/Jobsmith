// Functional test of src/common/overlay.js (Firefox in-page docked panel).
const { loadDom, evalScript, report } = require("./helpers");

const dom = loadDom("<!DOCTYPE html><html><body><h1>ATS form</h1></body></html>");
const { window } = dom;
const doc = window.document;

// Stub the extension messaging API the overlay uses on close.
const messages = [];
window.browser = { runtime: { sendMessage: (m) => { messages.push(m); } } };

evalScript(window, "common/overlay.js");

const PANEL_URL = "moz-extension://abc/sidepanel.html?tabId=7&overlay=1";
window.__jobsmithMountOverlay(PANEL_URL);

const host = () => doc.getElementById("__jobsmith-assist__");
const iframe = () => host() && host().querySelector("iframe");

const checks = [];
checks.push(["panel mounts with iframe", !!host() && !!iframe() && iframe().src === PANEL_URL]);

// Idempotent: second mount doesn't duplicate
window.__jobsmithMountOverlay(PANEL_URL);
checks.push(["mount is idempotent", doc.querySelectorAll("#__jobsmith-assist__").length === 1]);

// Collapse hides the iframe and shrinks the host
const rail = host().firstChild;
const collapseBtn = rail.children[0];
const closeBtn = rail.children[1];
collapseBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
checks.push(["collapse hides iframe", iframe().style.getPropertyValue("display") === "none" && host().style.getPropertyValue("width") === "30px"]);
collapseBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
checks.push(["expand restores iframe", iframe().style.getPropertyValue("display") === "block"]);

// Close removes the host and notifies the background
closeBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
checks.push(["close removes panel", !host()]);
checks.push(["close notifies background", messages.some(m => m && m.type === "assist-overlay-closed")]);

// SPA re-render protection: after a fresh mount, removing the host re-mounts it
window.__jobsmithMountOverlay(PANEL_URL);
host().remove();
setTimeout(() => {
  checks.push(["SPA removal re-mounts panel", !!host()]);
  const fail = report(checks);
  if (fail) process.exit(1);
  console.log("\noverlay.js: all checks passed");
}, 50);
