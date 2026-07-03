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
  if (ext.sidePanel) {
    const [tab] = await new Promise(r => ext.tabs.query({ active: true, currentWindow: true }, r));
    try { await ext.sidePanel.open({ windowId: tab.windowId }); window.close(); }
    catch (e) { setStatus(`Open failed: ${e.message}`, "err"); }
  } else if (ext.sidebarAction) {
    ext.sidebarAction.open();
    window.close();
  } else {
    setStatus("Side panel not supported in this browser.", "err");
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
