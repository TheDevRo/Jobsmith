// A saved credential must never be sent to another backend origin.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

async function run(stored, origin, statuses = [200]) {
  const calls = [], writes = [];
  const context = {
    URL,
    JobsmithStorage: {
      get: async () => stored,
      set: async (value) => writes.push(value),
    },
    fetch: async (url, init) => {
      calls.push({ url, init });
      const status = statuses.shift();
      return { status, ok: status === 200, text: async () => "rejected",
        json: async () => ({ token: "new-persistent-token" }) };
    },
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../src/common/handshake.js"), "utf8"), context);
  const out = await context.JobsmithHandshake.assistCheckin({
    origin, sessionId: "session-1", setupToken: "session-setup-token",
  });
  return { out, calls, writes };
}

(async () => {
  for (const origin of ["http://127.0.0.1:9000", "https://other.example", "https://jobs.example:9443"]) {
    const r = await run({ backendUrl: "https://jobs.example", token: "old-secret" }, origin);
    assert(r.out.ok);
    assert.strictEqual(r.calls[0].init.headers["X-Jobsmith-Token"], "session-setup-token");
    assert.strictEqual(r.calls[0].init.redirect, "error");
    assert.strictEqual(r.writes[0].backendUrl, origin);
    assert.strictEqual(r.writes[0].token, "new-persistent-token");
  }
  const same = await run({ backendUrl: "https://jobs.example/", token: "saved-secret" }, "https://jobs.example");
  assert.strictEqual(same.calls[0].init.headers["X-Jobsmith-Token"], "saved-secret");
  const stale = await run({ backendUrl: "https://jobs.example", token: "stale-secret" }, "https://jobs.example", [401, 200]);
  assert.strictEqual(stale.calls[1].init.headers["X-Jobsmith-Token"], "session-setup-token");
  const unbound = await run({ token: "unbound-secret" }, "http://localhost:8888");
  assert.strictEqual(unbound.calls[0].init.headers["X-Jobsmith-Token"], "session-setup-token");
  const rejected = await run({ backendUrl: "https://jobs.example", token: "saved-secret" }, "https://other.example", [401]);
  assert(!rejected.out.ok);
  assert.strictEqual(rejected.writes.length, 0);
  console.log("PASS handshake: cross-origin isolation, redirects, same-origin reuse, stale-token recovery, rejected pairing");
})().catch((e) => { console.error(e); process.exitCode = 1; });
