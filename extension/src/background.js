// background.js — service worker (Chrome) / background script (Firefox).
// Handles toolbar/side-panel wiring AND the Applicant Assist handoff:
// when a tab navigates to http(s)://(localhost|127.0.0.1)[:port]/assist/launch/<id>,
// the background fetches the per-session setup token, persists it into
// extension storage if missing or stale, then POSTs /api/ext/assist/checkin so
// the launch page can detect the extension and redirect to the apply URL.
//
// We do this from the background instead of via a content_script because
// Firefox MV3 treats host_permissions for localhost as user-opt-in, which
// blocks content_scripts on those origins until manually toggled. The
// background script can still fetch those URLs as long as host_permissions
// are present (which is also opt-in but, empirically, more reliable).

// Chrome MV3 runs this as a service worker (importScripts); Firefox MV3 lists
// the same files in manifest.background.scripts and loads them in order.
if (typeof importScripts === "function") {
  importScripts("common/storage.js", "common/handshake.js", "common/permissions.js");
}

const isChrome = typeof browser === "undefined";
const api = isChrome ? chrome : browser;

const Storage = globalThis.JobsmithStorage;
const Handshake = globalThis.JobsmithHandshake;
const Perms = globalThis.JobsmithPermissions;

api.runtime.onInstalled.addListener(() => {
  console.log("Jobsmith extension installed");
});

// ---------------------------------------------------------------------------
// Privileged-API RPC for the docked panel.
//
// Firefox gives an extension page iframed inside a web page (the overlay)
// only content-script-level privileges — no tabs, no scripting. The panel
// sends {type:"jobsmith-rpc"} messages and the background (always fully
// privileged) executes on its behalf. Functions can't cross the message
// boundary, so page-world calls go by NAME via "scripting.callInPage",
// invoking a window.__jobsmith* function with JSON args in the target's
// isolated world.
//
// The RPC hands attacker-influenceable input (target tab, script file, page
// function name) to privileged APIs, so it is locked down three ways: only
// our own extension pages may call it, only our own bundled scripts may be
// injected, and only window.__jobsmith* functions may be invoked.
// ---------------------------------------------------------------------------

// Scripts the panel is allowed to inject. Anything else — including a
// page-controlled path — is refused.
const RPC_ALLOWED_FILES = new Set([
  "common/snapshot.js",
  "common/fill.js",
  "common/dropcatch.js",
  "common/overlay.js",
]);
const RPC_ALLOWED_FN = /^__jobsmith[A-Za-z0-9_]*$/;

// Every message we accept must come from this extension...
function isOwnExtension(sender) {
  return !!sender && sender.id === api.runtime.id;
}
// ...and the RPC specifically must come from one of our own extension pages
// (popup / sidepanel), never from a content script or a web page.
function isOwnExtensionPage(sender) {
  if (!isOwnExtension(sender)) return false;
  const base = api.runtime.getURL("");
  return !!(sender.url && sender.url.startsWith(base));
}

function assertTarget(target) {
  if (!target || typeof target !== "object") throw new Error("bad rpc target");
  if (typeof target.tabId !== "number") throw new Error("bad rpc target");
  // Rebuild rather than pass through, so no extra InjectionTarget keys ride
  // along. allFrames and frameIds are mutually exclusive in Chrome.
  const out = { tabId: target.tabId };
  if (Array.isArray(target.frameIds)) {
    out.frameIds = target.frameIds.filter((n) => typeof n === "number");
  } else if (target.allFrames) {
    out.allFrames = true;
  }
  return out;
}

function assertFiles(files) {
  if (!Array.isArray(files) || !files.length) throw new Error("bad rpc files");
  for (const f of files) {
    if (!RPC_ALLOWED_FILES.has(f)) throw new Error("file not allowed: " + f);
  }
  return files;
}

api.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.type !== "jobsmith-rpc") return;
  if (!isOwnExtensionPage(sender)) {
    console.warn("[Jobsmith rpc]", "rejected message from", sender && (sender.url || sender.id));
    return;
  }
  (async () => {
    try {
      const arg = (msg.args && msg.args[0]) || {};
      let result;
      if (msg.method === "tabs.query") {
        result = await api.tabs.query(arg);
      } else if (msg.method === "tabs.get") {
        const tabId = msg.args && msg.args[0];
        if (typeof tabId !== "number") throw new Error("bad tabId");
        result = await api.tabs.get(tabId);
      } else if (msg.method === "scripting.executeScript") {
        result = await api.scripting.executeScript({
          target: assertTarget(arg.target),
          files: assertFiles(arg.files),
        });
      } else if (msg.method === "scripting.callInPage") {
        const { fnName, fnArgs } = arg;
        if (typeof fnName !== "string" || !RPC_ALLOWED_FN.test(fnName)) {
          throw new Error("fn not allowed: " + fnName);
        }
        result = await api.scripting.executeScript({
          target: assertTarget(arg.target),
          func: (name, a) => { const f = window[name]; return f ? f.apply(null, a) : null; },
          args: [fnName, Array.isArray(fnArgs) ? fnArgs : []],
        });
      } else {
        throw new Error("unknown rpc method: " + msg.method);
      }
      sendResponse({ ok: true, result });
    } catch (e) {
      sendResponse({ ok: false, error: String((e && e.message) || e) });
    }
  })();
  return true;  // keep the message channel open for the async response
});

// ---------------------------------------------------------------------------
// Applicant Assist handoff (background-driven)
// ---------------------------------------------------------------------------

// No port pin: the desktop backend binds a random free port when 8888 is
// already taken (e.g. a Docker Jobsmith is running).
const LAUNCH_RE = /^https?:\/\/(?:localhost|127\.0\.0\.1)(?::\d+)?\/assist\/launch\/([A-Za-z0-9_-]+)/;

// ---------------------------------------------------------------------------
// The in-page docked panel — the ONE panel implementation, both browsers.
//
// Tabs that completed an Assist handshake get the panel injected INTO the
// application page as a docked iframe (common/overlay.js), same UX as the
// old isolated-mode sidebar but in the user's own browser. Native
// sidebar/side-panel APIs are deliberately unused: Firefox's
// sidebarAction.open() only works from a real user-input handler (the
// status doesn't propagate through messages), and maintaining two panel
// surfaces meant duplicate behavior. The popup's "Open panel" button mounts
// the same overlay manually.
//
// An MV3 service worker is evicted after ~30s idle, which wipes any
// module-level state — the overlay would then stop re-mounting on later pages
// of a multi-step ATS flow. So both sets live in storage.session and are
// hydrated on every worker start.
// ---------------------------------------------------------------------------

const TABS_KEY = "assistTabs";
const SEEN_KEY = "seenSessions";
const SEEN_TTL_MS = 10 * 60 * 1000;

let assistTabs = new Set();          // tabIds with a live Assist panel
let seenSessions = new Map();        // sessionId -> first-seen epoch ms
let hydration = null;

function hydrate() {
  if (!hydration) {
    hydration = (async () => {
      try {
        const out = await Storage.sessionGet([TABS_KEY, SEEN_KEY]);
        if (Array.isArray(out[TABS_KEY])) assistTabs = new Set(out[TABS_KEY]);
        const seen = out[SEEN_KEY];
        if (seen && typeof seen === "object") {
          const now = Date.now();
          seenSessions = new Map(
            Object.entries(seen).filter(([, ts]) => now - ts < SEEN_TTL_MS)
          );
        }
      } catch (e) {
        console.warn("[Jobsmith state]", "hydrate failed:", (e && e.message) || e);
      }
    })();
  }
  return hydration;
}
hydrate();

async function persistTabs() {
  try { await Storage.sessionSet({ [TABS_KEY]: Array.from(assistTabs) }); }
  catch (e) { console.warn("[Jobsmith state]", "persist tabs failed:", (e && e.message) || e); }
}

async function persistSessions() {
  const now = Date.now();
  for (const [id, ts] of seenSessions) {
    if (now - ts >= SEEN_TTL_MS) seenSessions.delete(id);
  }
  try { await Storage.sessionSet({ [SEEN_KEY]: Object.fromEntries(seenSessions) }); }
  catch (e) { console.warn("[Jobsmith state]", "persist sessions failed:", (e && e.message) || e); }
}

// A site we don't hold an optional host permission for can't be scripted, and
// permissions.request() needs a user gesture we don't have here. Flag the tab
// so the toolbar icon reads as actionable; the popup does the asking.
function flagNeedsAccess(tabId, url) {
  try {
    api.action.setBadgeText({ tabId, text: "!" });
    if (api.action.setBadgeBackgroundColor) api.action.setBadgeBackgroundColor({ color: "#d9541e" });
    const host = (() => { try { return new URL(url).hostname; } catch (_) { return "this site"; } })();
    api.action.setTitle({ tabId, title: `Jobsmith needs access to ${host} — click to grant` });
  } catch (_) { /* action API unavailable — non-fatal */ }
}

function clearNeedsAccess(tabId) {
  try {
    api.action.setBadgeText({ tabId, text: "" });
    api.action.setTitle({ tabId, title: "Jobsmith" });
  } catch (_) { /* non-fatal */ }
}

async function injectAssistOverlay(tabId, url) {
  if (!(await Perms.hasSiteAccess(url))) {
    console.log("[Jobsmith overlay]", "no host permission for", url, "— prompting via toolbar");
    flagNeedsAccess(tabId, url);
    return;
  }
  clearNeedsAccess(tabId);
  try {
    const panelUrl = api.runtime.getURL("sidepanel.html")
      + "?tabId=" + encodeURIComponent(tabId) + "&overlay=1";
    await api.scripting.executeScript({ target: { tabId }, files: ["common/overlay.js"] });
    await api.scripting.executeScript({
      target: { tabId },
      func: (u) => { window.__jobsmithMountOverlay && window.__jobsmithMountOverlay(u); },
      args: [panelUrl],
    });
    console.log("[Jobsmith overlay]", "panel mounted in tab", tabId);
  } catch (e) {
    console.warn("[Jobsmith overlay]", "inject failed (non-fatal):", e && e.message || e);
  }
}

api.runtime.onMessage.addListener((msg, sender) => {
  if (!isOwnExtension(sender)) return;
  // User clicked ✕ on the docked panel — stop re-injecting for that tab.
  if (msg && msg.type === "assist-overlay-closed" && sender.tab) {
    hydrate().then(() => {
      assistTabs.delete(sender.tab.id);
      return persistTabs();
    });
  }
  // The popup just granted host access for a tab we had flagged — mount the
  // panel it couldn't mount before.
  if (msg && msg.type === "jobsmith-site-access-granted" && typeof msg.tabId === "number") {
    hydrate().then(async () => {
      clearNeedsAccess(msg.tabId);
      if (assistTabs.has(msg.tabId)) await injectAssistOverlay(msg.tabId, msg.url || "");
    });
  }
});

if (api.tabs && api.tabs.onRemoved) {
  api.tabs.onRemoved.addListener(async (tabId) => {
    await hydrate();
    if (assistTabs.delete(tabId)) await persistTabs();
  });
}

async function tryOpenSidePanel(tabId) {
  // Mark the tab; the overlay mounts once it navigates from the launch page
  // to the actual application (and re-mounts on every page of multi-step
  // flows).
  if (tabId == null) return;
  await hydrate();
  assistTabs.add(tabId);
  await persistTabs();
}

async function performAssistHandshake(launchUrl, sessionId, tabId) {
  await hydrate();
  if (seenSessions.has(sessionId)) return;
  seenSessions.set(sessionId, Date.now());
  await persistSessions();

  const forget = async () => {
    seenSessions.delete(sessionId);
    await persistSessions();
  };

  const origin = new URL(launchUrl).origin;
  const log = (...a) => console.log("[Jobsmith handshake]", ...a);
  log("detected launch URL, sessionId=", sessionId);

  // Auto-open the side panel so the user doesn't have to click the toolbar
  // icon → Open panel after every Apply Assist. Runs in parallel with the
  // checkin so a slow backend doesn't delay the panel.
  await tryOpenSidePanel(tabId);

  let setupToken;
  try {
    const metaResp = await fetch(
      origin + "/api/assist/session/" + encodeURIComponent(sessionId) + "/handshake-meta"
    );
    if (!metaResp.ok) {
      console.warn("[Jobsmith handshake]", "handshake-meta failed", metaResp.status);
      await forget();
      return;
    }
    const meta = await metaResp.json();
    setupToken = meta.setup_token;
  } catch (e) {
    console.error("[Jobsmith handshake]", "handshake-meta fetch threw", e);
    await forget();
    return;
  }

  const out = await Handshake.assistCheckin({ origin, sessionId, setupToken, log });
  if (!out.ok) await forget();
}

function maybeHandle(url, tabId) {
  if (!url) return;
  const m = LAUNCH_RE.exec(url);
  if (!m) return;
  performAssistHandshake(url, m[1], tabId);
}

if (api.tabs && api.tabs.onUpdated) {
  api.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
    if (changeInfo.url) maybeHandle(changeInfo.url, tabId);
    else if (changeInfo.status === "complete" && tab && tab.url) maybeHandle(tab.url, tabId);
    // Docked panel: (re)mount on every completed navigation of an assist
    // tab once it has left the launch page.
    if (
      changeInfo.status === "complete" &&
      tab && tab.url &&
      /^https?:/.test(tab.url) &&
      !LAUNCH_RE.test(tab.url)
    ) {
      await hydrate();
      if (assistTabs.has(tabId)) injectAssistOverlay(tabId, tab.url);
    }
  });
}
if (api.webNavigation && api.webNavigation.onCommitted) {
  api.webNavigation.onCommitted.addListener((details) => {
    if (details.frameId === 0) maybeHandle(details.url, details.tabId);
  });
}
