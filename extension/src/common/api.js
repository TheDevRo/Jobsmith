// api.js — shared client for the Jobsmith backend.
// Loaded from popup, sidepanel, and background contexts. No imports — uses globals.

const DEFAULT_BACKEND = "http://localhost:8888";

// Promise-shimmed storage — the one implementation, shared with the background
// and the content scripts (common/storage.js, loaded before this file).
const jobsmithStore = globalThis.JobsmithStorage;

async function jobsmithGetConfig() {
  const out = await jobsmithStore.get(["backendUrl", "token", "deepScan", "autoScan", "autoFill"]);
  return {
    backendUrl: (out.backendUrl || DEFAULT_BACKEND).replace(/\/+$/, ""),
    token: out.token || "",
    // deepScan: inject into every frame (slow on heavy pages but catches
    // ATS forms hosted in iframes). Default off — top-frame only; the side
    // panel automatically falls back to all-frames when the top frame has
    // no usable fields.
    deepScan: out.deepScan === true,
    // autoScan: let the panel scan/poll on its own (tab focus, switch,
    // navigation, and the active-job poll). Default on; flip off to make
    // the extension act only on explicit button clicks.
    autoScan: out.autoScan !== false,
    // autoFill: fill immediately after a successful auto-scan (once per
    // page URL). Default off — hands-off mode is opt-in.
    autoFill: out.autoFill === true,
  };
}

async function jobsmithSetConfig({ backendUrl, token, deepScan, autoScan, autoFill }) {
  const patch = {};
  if (backendUrl !== undefined) patch.backendUrl = backendUrl;
  if (token !== undefined) patch.token = token;
  if (deepScan !== undefined) patch.deepScan = !!deepScan;
  if (autoScan !== undefined) patch.autoScan = !!autoScan;
  if (autoFill !== undefined) patch.autoFill = !!autoFill;
  await jobsmithStore.set(patch);
}

// ---- Background fallback --------------------------------------------------
//
// In Firefox the docked panel is this extension page iframed into the job
// page, which gets only content-script-level privileges; combined with MV3
// treating host_permissions (localhost included) as user-opt-in, our fetches
// are attributed to the page origin and the backend's CORS allowlist rejects
// the preflight — surfacing as "NetworkError when attempting to fetch
// resource". The background script is always a top-level extension context,
// so it can make the same call successfully. We only pay this hop when the
// direct fetch actually fails.
function jobsmithExt() {
  return (typeof browser !== "undefined") ? browser
    : (typeof chrome !== "undefined") ? chrome : null;
}

function bgBackendFetch(path, { method = "GET", body } = {}) {
  const ext = jobsmithExt();
  return new Promise((resolve, reject) => {
    const handle = (resp) => {
      if (!resp) {
        const le = ext.runtime.lastError;
        reject(new Error(le ? le.message : "no response from background"));
      } else if (!resp.ok) {
        reject(new Error(resp.error));
      } else {
        resolve(resp.result);
      }
    };
    try {
      // Firefox's sendMessage rejects a callback as its second argument
      // (it reads that slot as `options`), so only Chrome gets the callback
      // shim; Firefox always returns a promise.
      const msg = { type: "jobsmith-rpc", method: "backend.fetch", args: [{ path, method, body }] };
      const p = (typeof browser === "undefined")
        ? ext.runtime.sendMessage(msg, handle)
        : ext.runtime.sendMessage(msg);
      if (p && typeof p.then === "function") p.then(handle, reject);
    } catch (e) {
      reject(e);
    }
  }).then((r) => {
    // Rebuild a real Response so every caller — including the raw/.blob()
    // resume-download path — is oblivious to which transport was used.
    const bin = atob(r.bodyB64 || "");
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new Response(bytes, {
      status: r.status,
      statusText: r.statusText,
      headers: { "content-type": r.contentType || "" },
    });
  });
}

// A failed direct fetch is worth retrying through the background only when it
// died at the network level (fetch rejects with a TypeError) — never for the
// HTTP-status Errors jobsmithFetch raises itself. Without a reachable
// runtime.sendMessage there's nothing to fall back to, so the original error
// stands.
function isNetworkError(e) {
  if (!(e instanceof TypeError)) return false;
  const ext = jobsmithExt();
  return !!(ext && ext.runtime && ext.runtime.sendMessage);
}

async function jobsmithFetch(path, { method = "GET", body, signal, raw = false } = {}) {
  const { backendUrl, token } = await jobsmithGetConfig();
  const headers = { "X-Jobsmith-Token": token };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let resp;
  try {
    resp = await fetch(backendUrl + path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
    });
  } catch (e) {
    if (!isNetworkError(e)) throw e;
    resp = await bgBackendFetch(path, { method, body });
  }
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    const err = new Error(`HTTP ${resp.status}: ${text || resp.statusText}`);
    err.status = resp.status;
    throw err;
  }
  if (raw) return resp;
  const ct = resp.headers.get("content-type") || "";
  return ct.includes("application/json") ? resp.json() : resp;
}

async function jobsmithFetchFile(path, filename) {
  const resp = await jobsmithFetch(path, { raw: true });
  const blob = await resp.blob();
  return new File([blob], filename, {
    type: blob.type || "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    lastModified: Date.now(),
  });
}

// ---- Workday one-tap auth -------------------------------------------------

// The Workday email + password (from the desktop config). Localhost-only; the
// password never leaves the machine except into the page the user is applying on.
async function jobsmithWorkdayCredentials() {
  return jobsmithFetch("/api/ext/workday_credentials");
}

// Registry state for a tenant host — whether an account is already known.
async function jobsmithWorkdayAccount(host) {
  return jobsmithFetch(`/api/ext/workday_account?host=${encodeURIComponent(host)}`);
}

// Report an auth success so every device remembers the tenant.
async function jobsmithReportWorkdayAccount(payload) {
  return jobsmithFetch("/api/ext/workday_account", { method: "POST", body: payload });
}

async function jobsmithHealth() {
  // Health is unauthenticated; call without a token so it still works
  // before the user has configured one.
  const { backendUrl } = await jobsmithGetConfig();
  let resp;
  try {
    resp = await fetch(backendUrl + "/api/ext/health");
  } catch (e) {
    // Same Firefox iframe/CORS fallback as jobsmithFetch. The background
    // sends the token header, which /api/ext/health simply ignores.
    if (!isNetworkError(e)) throw e;
    resp = await bgBackendFetch("/api/ext/health");
  }
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

// Expose to other extension scripts via window/global
this.Jobsmith = {
  jobsmithGetConfig, jobsmithSetConfig, jobsmithFetch, jobsmithFetchFile, jobsmithHealth,
  jobsmithWorkdayCredentials, jobsmithWorkdayAccount, jobsmithReportWorkdayAccount,
  DEFAULT_BACKEND,
};
