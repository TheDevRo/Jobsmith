// Functional tests of the docked-panel plumbing:
//   1. src/common/overlay.js — the in-page panel mount itself
//   2. src/background.js     — the privileged RPC the panel drives it through,
//                              specifically its sender/argument validation
// (Both live here because `npm test` enumerates test files by name.)
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

// ---------------------------------------------------------------------------
// background.js — RPC sender validation (SEC-10)
//
// The RPC executes scripts in arbitrary tabs on behalf of its caller, so it
// must accept messages ONLY from our own extension pages, inject ONLY our own
// bundled files, and call ONLY window.__jobsmith* page functions.
// ---------------------------------------------------------------------------

const EXT_ID = "jobsmith-ext";
const EXT_BASE = `chrome-extension://${EXT_ID}/`;

function loadBackground() {
  // Fresh realm: background.js picks the `chrome` namespace only when
  // `browser` is undefined, and the overlay test above defines `browser`.
  const bgDom = loadDom("<!DOCTYPE html><html><body></body></html>");
  const w = bgDom.window;

  const executed = [];
  const store = {};
  const area = {
    get: (keys) => Promise.resolve(Object.fromEntries(
      (Array.isArray(keys) ? keys : [keys]).map((k) => [k, store[k]]).filter(([, v]) => v !== undefined)
    )),
    set: (values) => { Object.assign(store, values); return Promise.resolve(); },
  };
  const msgListeners = [];

  w.chrome = {
    runtime: {
      id: EXT_ID,
      getURL: (p) => EXT_BASE + p,
      onInstalled: { addListener() {} },
      onMessage: { addListener: (fn) => msgListeners.push(fn) },
    },
    storage: { local: area, session: area },
    tabs: {
      query: async (info) => [{ id: 1, url: "https://boards.example.com/job", ...info }],
      get: async (id) => ({ id, url: "https://boards.example.com/job" }),
      onRemoved: { addListener() {} },
      onUpdated: { addListener() {} },
    },
    scripting: {
      executeScript: async (opts) => { executed.push(opts); return [{ result: "ok", frameId: 0 }]; },
    },
    action: { setBadgeText() {}, setBadgeBackgroundColor() {}, setTitle() {} },
    permissions: { contains: () => Promise.resolve(true), request: () => Promise.resolve(true) },
    webNavigation: { onCommitted: { addListener() {} } },
  };

  // Mirrors manifest.firefox.json's background.scripts order (Chrome does the
  // same via importScripts, which jsdom doesn't have).
  evalScript(w, "common/storage.js");
  evalScript(w, "common/handshake.js");
  evalScript(w, "common/permissions.js");
  evalScript(w, "background.js");

  // The RPC handler is the first listener background.js registers.
  const rpcListener = msgListeners[0];
  const rpc = (sender, msg) => new Promise((resolve) => {
    let answered = false;
    rpcListener(msg, sender, (resp) => { answered = true; resolve(resp); });
    setTimeout(() => { if (!answered) resolve(null); }, 60);  // null = never answered
  });
  return { rpc, executed };
}

const { rpc, executed } = loadBackground();

const panelSender = { id: EXT_ID, url: EXT_BASE + "sidepanel.html?tabId=1&overlay=1", tab: { id: 1 } };
const pageSender = { id: EXT_ID, url: "https://evil.example.com/", tab: { id: 2 } };
const foreignSender = { id: "some-other-extension", url: "chrome-extension://some-other-extension/x.html" };

const exec = (args) => ({ type: "jobsmith-rpc", method: "scripting.executeScript", args: [args] });
const callInPage = (args) => ({ type: "jobsmith-rpc", method: "scripting.callInPage", args: [args] });

(async () => {
  const okScan = await rpc(panelSender, exec({ target: { tabId: 1 }, files: ["common/snapshot.js"] }));
  checks.push(["panel may inject a bundled script", !!okScan && okScan.ok === true]);
  checks.push(["…and it reaches scripting.executeScript", executed.some((o) => o.files && o.files[0] === "common/snapshot.js")]);

  const okCall = await rpc(panelSender, callInPage({ target: { tabId: 1 }, fnName: "__jobsmithFillAndHighlight", fnArgs: [[], {}] }));
  checks.push(["panel may call a __jobsmith* page fn", !!okCall && okCall.ok === true]);

  const fromPage = await rpc(pageSender, exec({ target: { tabId: 2 }, files: ["common/snapshot.js"] }));
  checks.push(["web page sender is ignored", fromPage === null]);

  const fromOther = await rpc(foreignSender, exec({ target: { tabId: 1 }, files: ["common/snapshot.js"] }));
  checks.push(["other extension is ignored", fromOther === null]);

  const badFile = await rpc(panelSender, exec({ target: { tabId: 1 }, files: ["https://evil.example.com/x.js"] }));
  checks.push(["arbitrary file injection refused", !!badFile && badFile.ok === false && /not allowed/.test(badFile.error)]);

  const badFn = await rpc(panelSender, callInPage({ target: { tabId: 1 }, fnName: "eval", fnArgs: ["1"] }));
  checks.push(["non-__jobsmith fn refused", !!badFn && badFn.ok === false && /not allowed/.test(badFn.error)]);

  const badTarget = await rpc(panelSender, exec({ target: "everything", files: ["common/fill.js"] }));
  checks.push(["malformed target refused", !!badTarget && badTarget.ok === false && /target/.test(badTarget.error)]);

  checks.push(["nothing rejected ever reached scripting", !executed.some((o) =>
    (o.files || []).some((f) => !f.startsWith("common/")) || (o.args || [])[0] === "eval")]);

  checks.push(["SPA removal re-mounts panel", !!host()]);
  const fail = report(checks);
  if (fail) process.exit(1);
  console.log("\noverlay.js + background.js RPC: all checks passed");
})();
