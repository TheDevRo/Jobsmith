// api.js — shared client for the Jobsmith backend.
// Loaded from popup, sidepanel, and background contexts. No imports — uses globals.

const DEFAULT_BACKEND = "http://localhost:8888";

const jobsmithBrowser = (typeof browser !== "undefined") ? browser : chrome;

async function jobsmithGetConfig() {
  const out = await new Promise((resolve) => {
    jobsmithBrowser.storage.local.get(["backendUrl", "token", "deepScan", "autoScan"], resolve);
  });
  return {
    backendUrl: (out.backendUrl || DEFAULT_BACKEND).replace(/\/+$/, ""),
    token: out.token || "",
    // deepScan: inject into every frame (slow on heavy pages but catches
    // ATS forms hosted in iframes). Default off — top-frame only.
    deepScan: out.deepScan === true,
    // autoScan: let the panel scan/poll on its own (tab focus, switch,
    // navigation, and the active-job poll). Default on; flip off to make
    // the extension act only on explicit button clicks.
    autoScan: out.autoScan !== false,
  };
}

async function jobsmithSetConfig({ backendUrl, token, deepScan, autoScan }) {
  const patch = {};
  if (backendUrl !== undefined) patch.backendUrl = backendUrl;
  if (token !== undefined) patch.token = token;
  if (deepScan !== undefined) patch.deepScan = !!deepScan;
  if (autoScan !== undefined) patch.autoScan = !!autoScan;
  await new Promise((resolve) => {
    jobsmithBrowser.storage.local.set(patch, resolve);
  });
}

async function jobsmithFetch(path, { method = "GET", body, signal, raw = false } = {}) {
  const { backendUrl, token } = await jobsmithGetConfig();
  const headers = { "X-Jobsmith-Token": token };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(backendUrl + path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    signal,
  });
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

async function jobsmithHealth() {
  // Health is unauthenticated; call without a token so it still works
  // before the user has configured one.
  const { backendUrl } = await jobsmithGetConfig();
  const resp = await fetch(backendUrl + "/api/ext/health");
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

// Expose to other extension scripts via window/global
this.Jobsmith = { jobsmithGetConfig, jobsmithSetConfig, jobsmithFetch, jobsmithFetchFile, jobsmithHealth, DEFAULT_BACKEND };
