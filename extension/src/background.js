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

const isChrome = typeof browser === "undefined";
const api = isChrome ? chrome : browser;

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
// ---------------------------------------------------------------------------

api.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.type !== "jobsmith-rpc") return;
  (async () => {
    try {
      let result;
      if (msg.method === "tabs.query") {
        result = await api.tabs.query(msg.args[0]);
      } else if (msg.method === "tabs.get") {
        result = await api.tabs.get(msg.args[0]);
      } else if (msg.method === "scripting.executeScript") {
        const { target, files } = msg.args[0];
        result = await api.scripting.executeScript({ target, files });
      } else if (msg.method === "scripting.callInPage") {
        const { target, fnName, fnArgs } = msg.args[0];
        result = await api.scripting.executeScript({
          target,
          func: (name, a) => { const f = window[name]; return f ? f.apply(null, a) : null; },
          args: [fnName, fnArgs || []],
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
const seenSessions = new Set();

function storageGet(keys) {
  const r = api.storage.local.get(keys);
  if (r && typeof r.then === "function") return r;
  return new Promise((resolve) => api.storage.local.get(keys, resolve));
}
function storageSet(values) {
  const r = api.storage.local.set(values);
  if (r && typeof r.then === "function") return r;
  return new Promise((resolve) => api.storage.local.set(values, resolve));
}

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
// ---------------------------------------------------------------------------

const assistTabs = new Set();

async function injectAssistOverlay(tabId) {
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
  // User clicked ✕ on the docked panel — stop re-injecting for that tab.
  if (msg && msg.type === "assist-overlay-closed" && sender.tab) {
    assistTabs.delete(sender.tab.id);
  }
});

if (api.tabs && api.tabs.onRemoved) {
  api.tabs.onRemoved.addListener((tabId) => assistTabs.delete(tabId));
}

function tryOpenSidePanel(tabId) {
  // Mark the tab; the overlay mounts once it navigates from the launch page
  // to the actual application (and re-mounts on every page of multi-step
  // flows).
  if (tabId != null) assistTabs.add(tabId);
}

async function performAssistHandshake(launchUrl, sessionId, tabId) {
  if (seenSessions.has(sessionId)) return;
  seenSessions.add(sessionId);

  const origin = new URL(launchUrl).origin;
  console.log("[Jobsmith handshake]", "detected launch URL, sessionId=", sessionId);

  // Auto-open the side panel so the user doesn't have to click the toolbar
  // icon → Open panel after every Apply Assist. Runs in parallel with the
  // checkin so a slow backend doesn't delay the panel.
  tryOpenSidePanel(tabId);

  let setupToken, applyUrl;
  try {
    const metaResp = await fetch(
      origin + "/api/assist/session/" + encodeURIComponent(sessionId) + "/handshake-meta"
    );
    if (!metaResp.ok) {
      console.warn("[Jobsmith handshake]", "handshake-meta failed", metaResp.status);
      seenSessions.delete(sessionId);
      return;
    }
    const meta = await metaResp.json();
    setupToken = meta.setup_token;
    applyUrl = meta.apply_url;
  } catch (e) {
    console.error("[Jobsmith handshake]", "handshake-meta fetch threw", e);
    seenSessions.delete(sessionId);
    return;
  }

  const stored = (await storageGet(["backendUrl", "token"])) || {};
  const hasToken = !!stored.token;

  async function checkin(tok) {
    const r = await fetch(origin + "/api/ext/assist/checkin", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Jobsmith-Token": tok },
      body: JSON.stringify({ session_id: sessionId, had_token: hasToken }),
    });
    console.log("[Jobsmith handshake]", "checkin status:", r.status);
    return r;
  }

  let token = hasToken ? stored.token : setupToken;
  try {
    let r = await checkin(token);
    if (r.status === 401 && token !== setupToken) {
      // Stored token is stale (backend token rotated, or a different Jobsmith
      // instance). The page's setup token is authoritative for this session.
      console.warn("[Jobsmith handshake]", "stored token rejected; retrying with setup token");
      token = setupToken;
      r = await checkin(token);
    }
    if (!r.ok) {
      seenSessions.delete(sessionId);
      return;
    }
  } catch (e) {
    console.error("[Jobsmith handshake]", "checkin fetch threw", e);
    seenSessions.delete(sessionId);
    return;
  }

  // Persist whatever just worked so the popup/side panel talk to the same
  // backend with a valid token (heals rotated tokens and moved ports).
  if (stored.token !== token || stored.backendUrl !== origin) {
    try {
      await storageSet({ backendUrl: origin, token });
      console.log("[Jobsmith handshake]", "persisted backendUrl + token");
    } catch (e) {
      console.warn("[Jobsmith handshake]", "storage.set failed", e);
    }
  }
}

function maybeHandle(url, tabId) {
  if (!url) return;
  const m = LAUNCH_RE.exec(url);
  if (!m) return;
  performAssistHandshake(url, m[1], tabId);
}

if (api.tabs && api.tabs.onUpdated) {
  api.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (changeInfo.url) maybeHandle(changeInfo.url, tabId);
    else if (changeInfo.status === "complete" && tab && tab.url) maybeHandle(tab.url, tabId);
    // Docked panel: (re)mount on every completed navigation of an assist
    // tab once it has left the launch page.
    if (
      changeInfo.status === "complete" &&
      assistTabs.has(tabId) &&
      tab && tab.url &&
      /^https?:/.test(tab.url) &&
      !LAUNCH_RE.test(tab.url)
    ) {
      injectAssistOverlay(tabId);
    }
  });
}
if (api.webNavigation && api.webNavigation.onCommitted) {
  api.webNavigation.onCommitted.addListener((details) => {
    if (details.frameId === 0) maybeHandle(details.url, details.tabId);
  });
}
