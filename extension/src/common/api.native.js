// api.native.js — iOS-standalone replacement for api.js.
// Same exported surface (this.Jobsmith.*), but instead of HTTP calls to a
// self-hosted backend, every operation is a browser.runtime.sendNativeMessage
// to the containing Jobsmith app (SafariWebExtensionHandler), which serves
// profile/job/DOCX/field-mapping data from the shared App Group container.
//
// The build script installs this file AS common/api.js in the iOS-standalone
// extension bundle; sidepanel.js/popup.js call sites stay unchanged.

const DEFAULT_BACKEND = "app://jobsmith";

const jobsmithBrowser = (typeof browser !== "undefined") ? browser : chrome;

async function jobsmithGetConfig() {
  const out = await new Promise((resolve) => {
    jobsmithBrowser.storage.local.get(["deepScan", "autoScan", "autoFill"], resolve);
  });
  return {
    // No backend/token in standalone mode — kept for call-site compatibility.
    backendUrl: DEFAULT_BACKEND,
    token: "native",
    deepScan: out.deepScan === true,
    autoScan: out.autoScan !== false,
    autoFill: out.autoFill === true,
  };
}

async function jobsmithSetConfig({ deepScan, autoScan, autoFill }) {
  const patch = {};
  if (deepScan !== undefined) patch.deepScan = !!deepScan;
  if (autoScan !== undefined) patch.autoScan = !!autoScan;
  if (autoFill !== undefined) patch.autoFill = !!autoFill;
  await new Promise((resolve) => {
    jobsmithBrowser.storage.local.set(patch, resolve);
  });
}

async function jobsmithNative(name, body) {
  const reply = await jobsmithBrowser.runtime.sendNativeMessage(
    "com.thedevro.jobsmith.standalone.Assist",
    { name, body: body || {} }
  );
  if (!reply) throw new Error("No response from Jobsmith app");
  if (reply.error) {
    const err = new Error(reply.error);
    err.status = reply.status || 500;
    throw err;
  }
  return reply.result;
}

// Translate the backend paths the extension UI uses into native messages, so
// sidepanel.js/popup.js don't need to change.
const NATIVE_ROUTES = [
  { re: /^\/api\/ext\/health$/, name: () => ["health", {}] },
  { re: /^\/api\/ext\/profile$/, name: () => ["getProfile", {}] },
  { re: /^\/api\/ext\/job\/([^/]+)\/mark-applied$/, name: (m) => ["markApplied", { jobId: m[1] }] },
  { re: /^\/api\/ext\/job\/([^/]+)$/, name: (m) => ["getJob", { jobId: m[1] }] },
  { re: /^\/api\/ext\/scan$/, name: (m, body) => ["scan", body] },
  { re: /^\/api\/ext\/answer$/, name: (m, body) => ["answer", body] },
  { re: /^\/api\/extension\/active-job$/, name: () => ["getActiveJob", {}] },
  // Desktop-only cookie sync: succeed as a no-op so the popup doesn't error.
  { re: /^\/api\/ext\/sessions\/import$/, name: () => ["noop", {}] },
];

async function jobsmithFetch(path, { body } = {}) {
  const cleanPath = path.split("?")[0];
  for (const route of NATIVE_ROUTES) {
    const m = cleanPath.match(route.re);
    if (m) {
      const [name, msgBody] = route.name(m, body);
      return jobsmithNative(name, msgBody);
    }
  }
  throw new Error(`No native route for ${path}`);
}

const FILE_ROUTES = [
  { re: /^\/api\/ext\/resume\/([^/]+)$/, name: "getResumeFile" },
  { re: /^\/api\/ext\/cover-letter\/([^/]+)$/, name: "getCoverFile" },
];

function base64ToBytes(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

async function jobsmithFetchFile(path, filename) {
  const cleanPath = path.split("?")[0];
  for (const route of FILE_ROUTES) {
    const m = cleanPath.match(route.re);
    if (m) {
      const result = await jobsmithNative(route.name, { jobId: m[1] });
      const bytes = base64ToBytes(result.base64);
      return new File([bytes], result.filename || filename, {
        type: result.mime || "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        lastModified: Date.now(),
      });
    }
  }
  throw new Error(`No native file route for ${path}`);
}

async function jobsmithHealth() {
  return jobsmithNative("health", {});
}

// Expose to other extension scripts via window/global
this.Jobsmith = { jobsmithGetConfig, jobsmithSetConfig, jobsmithFetch, jobsmithFetchFile, jobsmithHealth, DEFAULT_BACKEND, jobsmithNative };
