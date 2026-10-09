const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { loadDom, evalScript } = require("./helpers");

(async () => {
  const calls = [];
  const event = { addListener() {} };
  const store = { backendUrl: "https://jobs.example:9443", token: "saved-token" };
  const context = {
    URL, console, JobsmithStorage: { get: async () => store,
      sessionGet: async () => ({}), sessionSet: async () => {} },
    JobsmithHandshake: {}, JobsmithPermissions: {},
    chrome: {
      runtime: { id: "test", getURL: (p) => "chrome-extension://test/" + p,
        onInstalled: event, onMessage: event },
      tabs: { onUpdated: event, onRemoved: event },
      webNavigation: { onCommitted: event },
      scripting: { executeScript: async (arg) => calls.push(arg) },
    },
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../src/background.js"), "utf8"), context);
  const perform = context.performAssistHandshake;
  context.performAssistHandshake = async (...args) => calls.push(args);
  await context.maybeHandle("https://jobs.example:9443/assist/launch/session-1", 10);
  assert.strictEqual(calls.length, 1, "configured remote backend must launch Assist");
  assert.strictEqual(calls[0][1], "session-1");
  for (const url of ["https://other.example/assist/launch/session-2",
    "https://jobs.example/assist/launch/session-2", "https://jobs.example:9443/other"]) {
    await context.maybeHandle(url, 10);
  }
  assert.strictEqual(calls.length, 1, "other origins/ports/pages must be ignored");
  await context.maybeHandle("http://127.0.0.1:8888/assist/launch/local", 11);
  assert.strictEqual(calls.length, 2, "local desktop handoff must still work");
  // The remote worker injects the handshake in the logged-in tab, without
  // trying to read the loopback-only metadata endpoint using worker cookies.
  context.performAssistHandshake = perform;
  context.fetch = async () => { throw new Error("remote worker must not fetch metadata"); };
  await context.maybeHandle("https://jobs.example:9443/assist/launch/session-3", 10);
  assert.deepStrictEqual(Array.from(calls[2].files), [
    "common/storage.js", "common/handshake.js", "assist_handshake.js",
  ]);

  const dom = loadDom(fs.readFileSync(path.join(__dirname, "../src/popup.html"), "utf8"));
  const w = dom.window;
  const permissions = [], writes = [];
  w.Jobsmith = {
    DEFAULT_BACKEND: "http://localhost:8888",
    jobsmithGetConfig: async () => ({ ...store }),
    jobsmithSetConfig: async (value) => writes.push(value),
  };
  w.JobsmithPermissions = { hasSiteAccess: async () => true,
    requestSiteAccess: async (url) => { permissions.push(url); return true; } };
  w.chrome = { tabs: { query: (_, cb) => cb([]) } };
  evalScript(w, "popup.js");
  await new Promise((r) => setImmediate(r));
  w.document.getElementById("backendUrl").value = "https://jobs.example:9443/";
  w.document.getElementById("save").click();
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(permissions[0], "https://jobs.example:9443");
  assert.strictEqual(writes[0].backendUrl, "https://jobs.example:9443");
  for (const url of ["http://jobs.example", "https://user:password@jobs.example", "https://jobs.example/api"]) {
    w.document.getElementById("backendUrl").value = url;
    w.document.getElementById("save").click();
    await new Promise((r) => setImmediate(r));
  }
  assert.strictEqual(writes.length, 1, "unsafe remote URLs must not be saved");
  w.document.getElementById("backendUrl").value = "https://new-instance.example";
  w.document.getElementById("save").click();
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(writes[1].token, "", "changing the URL must clear the previous instance's token");
  w.JobsmithPermissions.requestSiteAccess = async () => false;
  w.document.getElementById("backendUrl").value = "https://denied.example";
  w.document.getElementById("save").click();
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(writes.length, 2, "denied permissions must not change configuration");
  w.close();
  console.log("PASS remote backend: exact-origin handoff, local compatibility, host permission, safe URL configuration");
})().catch((e) => { console.error(e); process.exitCode = 1; });
