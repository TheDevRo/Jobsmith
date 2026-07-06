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

if (isChrome && api.sidePanel) {
  api.sidePanel.setPanelBehavior({ openPanelOnActionClick: false })
    .catch((e) => console.warn("sidePanel.setPanelBehavior:", e));
}

api.runtime.onInstalled.addListener(() => {
  console.log("Jobsmith extension installed");
});

api.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "open-side-panel" && isChrome && api.sidePanel) {
    api.sidePanel.open({ windowId: sender.tab?.windowId })
      .then(() => sendResponse({ ok: true }))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
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

// Firefox: the id of the detached panel window we opened, if it's still up.
let panelWindowId = null;
if (typeof browser !== "undefined" && api.windows && api.windows.onRemoved) {
  api.windows.onRemoved.addListener((wid) => {
    if (wid === panelWindowId) panelWindowId = null;
  });
}

async function getTab(tabId) {
  if (tabId == null || !api.tabs || !api.tabs.get) return null;
  try {
    return await (api.tabs.get.length === 1
      ? api.tabs.get(tabId)
      : new Promise((res, rej) => api.tabs.get(tabId, (t) =>
          api.runtime.lastError ? rej(api.runtime.lastError) : res(t))));
  } catch (_) {
    return null;
  }
}

async function tryOpenSidePanel(tabId) {
  if (isChrome && api.sidePanel && api.sidePanel.open) {
    // Chrome: sidePanel.open() accepts the launch-URL navigation as a
    // downstream user gesture.
    try {
      const tab = await getTab(tabId);
      const windowId = tab && tab.windowId;
      await api.sidePanel.open(windowId != null ? { windowId } : {});
      console.log("[Jobsmith handshake]", "side panel opened");
    } catch (e) {
      console.warn("[Jobsmith handshake]", "auto-open side panel failed (non-fatal):", e && e.message || e);
    }
    return;
  }

  // Firefox: sidebarAction.open() only works from a real user-input handler,
  // and that status does NOT propagate through content-script messages — so
  // the native sidebar cannot be opened from a navigation event, ever.
  // windows.create() has no such restriction: open the panel UI as a
  // detached popup window pinned to the assist tab.
  if (!api.windows || !api.windows.create) return;
  try {
    if (panelWindowId != null) {
      // A panel from a previous Assist session may be pinned to a stale tab —
      // replace it so it tracks the new application tab.
      try { await api.windows.remove(panelWindowId); } catch (_) {}
      panelWindowId = null;
    }
    const url = api.runtime.getURL("sidepanel.html") +
      (tabId != null ? "?tabId=" + encodeURIComponent(tabId) : "");
    const win = await api.windows.create({ url, type: "popup", width: 430, height: 760 });
    panelWindowId = win && win.id;
    console.log("[Jobsmith handshake]", "panel window opened", panelWindowId);
    // Hand focus back to the browser window hosting the application tab so
    // the user can start typing/reviewing immediately.
    const tab = await getTab(tabId);
    if (tab && tab.windowId != null && api.windows.update) {
      try { await api.windows.update(tab.windowId, { focused: true }); } catch (_) {}
    }
  } catch (e) {
    console.warn("[Jobsmith handshake]", "panel window failed (non-fatal):", e && e.message || e);
  }
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
  });
}
if (api.webNavigation && api.webNavigation.onCommitted) {
  api.webNavigation.onCommitted.addListener((details) => {
    if (details.frameId === 0) maybeHandle(details.url, details.tabId);
  });
}
