// popup.js — config screen for the extension.

const $ = (id) => document.getElementById(id);
const ext = (typeof browser !== "undefined") ? browser : chrome;

function setStatus(msg, cls = "") {
  const s = $("status");
  s.textContent = msg;
  s.className = cls;
}

async function loadCurrent() {
  const cfg = await Jobsmith.jobsmithGetConfig();
  $("backendUrl").value = cfg.backendUrl;
  $("token").value = cfg.token;
  $("deepScan").checked = !!cfg.deepScan;
}

function currentTab() {
  return new Promise((r) => ext.tabs.query({ active: true, currentWindow: true }, (tabs) => r(tabs && tabs[0])));
}

// ---------------------------------------------------------------------------
// Token onboarding — the backend hands its token to any loopback caller, so
// the popup can fetch it instead of making the user hunt for the file.
// ---------------------------------------------------------------------------

async function probeBackend(url) {
  const base = url.replace(/\/+$/, "");
  const health = await fetch(base + "/api/ext/health");
  if (!health.ok) throw new Error(`HTTP ${health.status}`);
  const resp = await fetch(base + "/api/extension/token");
  if (!resp.ok) throw new Error(`token: HTTP ${resp.status}`);
  const data = await resp.json();
  if (!data || !data.token) throw new Error("backend returned no token");
  return { base, token: data.token };
}

$("detect").addEventListener("click", async () => {
  setStatus("Looking for Jobsmith…");
  const candidates = [];
  const typed = $("backendUrl").value.trim();
  if (typed) candidates.push(typed);
  for (const u of [Jobsmith.DEFAULT_BACKEND, "http://127.0.0.1:8888"]) {
    if (!candidates.includes(u)) candidates.push(u);
  }
  for (const url of candidates) {
    try {
      const { base, token } = await probeBackend(url);
      $("backendUrl").value = base;
      $("token").value = token;
      await Jobsmith.jobsmithSetConfig({ backendUrl: base, token });
      setStatus(`Connected to ${base}. Token saved.`, "ok");
      return;
    } catch (_) { /* try the next candidate */ }
  }
  setStatus(
    "No Jobsmith backend found on localhost:8888. Start the app (or set the " +
    "URL above if it's on another port), then try again.",
    "err",
  );
});

$("openSettings").addEventListener("click", () => {
  const base = ($("backendUrl").value.trim() || Jobsmith.DEFAULT_BACKEND).replace(/\/+$/, "");
  ext.tabs.create({ url: base + "/#settings" });
});

// ---------------------------------------------------------------------------
// Site access — the manifest only carries localhost + LinkedIn + Indeed; every
// other ATS domain is an optional host permission the user grants here (the
// only place we have the user gesture Chrome/Firefox require).
// ---------------------------------------------------------------------------

let accessTab = null;

async function refreshSiteAccess() {
  const box = $("siteAccess");
  const tab = await currentTab();
  accessTab = tab || null;
  const url = (tab && tab.url) || "";
  if (!/^https?:/.test(url) || await JobsmithPermissions.hasSiteAccess(url)) {
    box.hidden = true;
    return;
  }
  try { $("siteHost").textContent = new URL(url).hostname; } catch (_) {}
  box.hidden = false;
}

$("grantSite").addEventListener("click", async () => {
  const url = (accessTab && accessTab.url) || "";
  const granted = await JobsmithPermissions.requestSiteAccess(url);
  if (!granted) {
    setStatus("Access not granted.", "warn");
    return;
  }
  $("siteAccess").hidden = true;
  setStatus("Access granted for this site.", "ok");
  // Let the background clear the toolbar flag and mount the panel it couldn't
  // mount during the Assist handoff.
  try {
    ext.runtime.sendMessage({ type: "jobsmith-site-access-granted", tabId: accessTab.id, url });
  } catch (_) { /* non-fatal */ }
});

$("save").addEventListener("click", async () => {
  await Jobsmith.jobsmithSetConfig({
    backendUrl: $("backendUrl").value.trim() || Jobsmith.DEFAULT_BACKEND,
    token: $("token").value.trim(),
    deepScan: $("deepScan").checked,
  });
  setStatus("Saved.", "ok");
});

$("test").addEventListener("click", async () => {
  setStatus("Checking…");
  try {
    await Jobsmith.jobsmithHealth();
  } catch (e) {
    setStatus(`Backend unreachable: ${e.message}`, "err");
    return;
  }
  try {
    await Jobsmith.jobsmithFetch("/api/ext/profile");
    setStatus("Connected. Token OK.", "ok");
  } catch (e) {
    if (e.status === 401) setStatus("Backend reachable, but token is wrong.", "err");
    else setStatus(`Error: ${e.message}`, "err");
  }
});

$("open").addEventListener("click", async () => {
  // Mount the in-page docked panel on the active tab — the single panel
  // implementation for both browsers (see background.js).
  //
  // The toolbar click itself grants activeTab, so the injection below works
  // even without a host permission for this domain — but only until the page
  // navigates. Ask for the persistent permission first (this handler is the
  // user gesture Chrome/Firefox require); carry on either way.
  const tab = accessTab || await currentTab();
  if (!tab || !tab.id) { setStatus("No active tab.", "err"); return; }
  if (!/^https?:/.test(tab.url || "")) {
    setStatus("Open a job page first — the panel attaches to the page.", "err");
    return;
  }
  if (!(await JobsmithPermissions.hasSiteAccess(tab.url))) {
    await JobsmithPermissions.requestSiteAccess(tab.url);
  }
  try {
    const panelUrl = ext.runtime.getURL("sidepanel.html") + "?tabId=" + tab.id + "&overlay=1";
    await ext.scripting.executeScript({ target: { tabId: tab.id }, files: ["common/overlay.js"] });
    await ext.scripting.executeScript({
      target: { tabId: tab.id },
      func: (u) => { window.__jobsmithMountOverlay && window.__jobsmithMountOverlay(u); },
      args: [panelUrl],
    });
    window.close();
  } catch (e) {
    setStatus(`Open failed: ${e.message}`, "err");
  }
});

// ---------------------------------------------------------------------------
// Session sync — grab cookies for a domain from the user's browser and POST
// them to the backend, which converts them to a Playwright session.
// ---------------------------------------------------------------------------

function getCookies(domain) {
  return new Promise((resolve, reject) => {
    try {
      ext.cookies.getAll({ domain }, (cookies) => {
        const err = ext.runtime.lastError;
        if (err) reject(new Error(err.message));
        else resolve(cookies || []);
      });
    } catch (e) { reject(e); }
  });
}

async function syncSession(label, backendDomain, cookieDomain) {
  setStatus(`Reading ${label} cookies…`);
  let cookies;
  try {
    cookies = await getCookies(cookieDomain);
  } catch (e) {
    setStatus(`Cookie read failed: ${e.message}`, "err");
    return;
  }
  if (!cookies.length) {
    setStatus(`No ${label} cookies found — sign in to ${label} in this browser first.`, "err");
    return;
  }
  setStatus(`Sending ${cookies.length} ${label} cookies…`);
  try {
    const resp = await Jobsmith.jobsmithFetch("/api/ext/sessions/import", {
      method: "POST",
      body: { domain: backendDomain, cookies },
    });
    setStatus(`${label} session synced (${resp.cookie_count} cookies).`, "ok");
  } catch (e) {
    setStatus(`${label} sync failed: ${e.message}`, "err");
  }
}

$("syncLinkedin").addEventListener("click",
  () => syncSession("LinkedIn", "linkedin", ".linkedin.com"));
$("syncIndeed").addEventListener("click",
  () => syncSession("Indeed", "indeed", ".indeed.com"));

loadCurrent();
refreshSiteAccess();
