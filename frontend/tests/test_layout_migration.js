// One-shot migration off the retired app-wide layout mode.
//
// Before the UI consolidation, `jobsmith_layout` = 'deck' | 'classic' swapped
// the WHOLE app between the card/board rendering and the list/tab rendering.
// It is gone; each screen now stores its own choice. core.js must translate a
// legacy value into the equivalent pair of per-view preferences exactly once,
// must not clobber a per-view choice the user already made, and must then drop
// the old key so the migration never runs twice.
//
// core.js is eval'd fresh per case (its migration runs at load), with a
// localStorage stub seeded to the state under test.
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const CORE_JS = fs.readFileSync(path.join(__dirname, "..", "js", "core.js"), "utf8");

function report(checks) {
  let fail = 0;
  for (const [name, ok] of checks) {
    console.log((ok ? "PASS" : "FAIL") + "  " + name);
    if (!ok) fail++;
  }
  return fail;
}

// Run core.js against a fresh document with `seed` as the entire localStorage,
// and hand back the resulting store.
function runMigration(seed) {
  const virtualConsole = new VirtualConsole();
  const dom = new JSDOM(`<!DOCTYPE html><html><body></body></html>`, {
    runScripts: "dangerously", url: "http://localhost:8888/", virtualConsole,
  });
  const store = Object.assign(Object.create(null), seed);
  Object.defineProperty(dom.window, "localStorage", {
    configurable: true,
    value: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: (k) => { delete store[k]; },
      clear: () => { for (const k of Object.keys(store)) delete store[k]; },
    },
  });
  dom.window.eval(CORE_JS);
  return store;
}

const checks = [];
const has = (s, k) => Object.prototype.hasOwnProperty.call(s, k);

// ---- Legacy 'classic': both screens land on their list/table rendering ----
const classic = runMigration({ jobsmith_layout: "classic" });
checks.push(["classic → inbox view becomes 'list'", classic.jobsmith_inbox_view === "list"]);
checks.push(["classic → pipeline view becomes 'table'", classic.jobsmith_pipeline_view === "table"]);
checks.push(["classic → the legacy key is removed", !has(classic, "jobsmith_layout")]);

// ---- Legacy 'deck': the defaults already ARE the deck rendering, so the
//      migration writes nothing and simply drops the key. ----
const deck = runMigration({ jobsmith_layout: "deck" });
checks.push(["deck → no inbox view is written (default is cards)", !has(deck, "jobsmith_inbox_view")]);
checks.push(["deck → no pipeline view is written (default is board)", !has(deck, "jobsmith_pipeline_view")]);
checks.push(["deck → the legacy key is removed", !has(deck, "jobsmith_layout")]);

// ---- An unrecognised legacy value is treated like 'deck' (defaults kept). ----
const junk = runMigration({ jobsmith_layout: "whatever" });
checks.push(["unknown legacy value keeps the defaults",
  !has(junk, "jobsmith_inbox_view") && !has(junk, "jobsmith_pipeline_view")]);
checks.push(["unknown legacy value still drops the key", !has(junk, "jobsmith_layout")]);

// ---- An explicit per-view choice always wins over the legacy mode. ----
const mixed = runMigration({
  jobsmith_layout: "classic",
  jobsmith_inbox_view: "cards",       // user already chose cards
  jobsmith_pipeline_view: "board",    // ...and the board
});
checks.push(["existing inbox choice is not clobbered", mixed.jobsmith_inbox_view === "cards"]);
checks.push(["existing pipeline choice is not clobbered", mixed.jobsmith_pipeline_view === "board"]);
checks.push(["mixed → the legacy key is still removed", !has(mixed, "jobsmith_layout")]);

// A half-migrated store fills in only the missing half.
const half = runMigration({ jobsmith_layout: "classic", jobsmith_inbox_view: "cards" });
checks.push(["half-set store keeps the chosen inbox view", half.jobsmith_inbox_view === "cards"]);
checks.push(["half-set store still fills in the pipeline view", half.jobsmith_pipeline_view === "table"]);

// ---- Fresh install: nothing to migrate, nothing written. ----
const fresh = runMigration({});
checks.push(["fresh install writes no view keys",
  !has(fresh, "jobsmith_inbox_view") && !has(fresh, "jobsmith_pipeline_view")]);

// ---- Idempotent: a second load after migrating must not re-apply anything.
//      Simulate by feeding the migrated 'classic' store back through, minus the
//      legacy key, after the user has since flipped the Inbox back to cards. ----
const after = Object.assign({}, classic, { jobsmith_inbox_view: "cards" });
const rerun = runMigration(after);
checks.push(["second load leaves the user's later choice alone", rerun.jobsmith_inbox_view === "cards"]);
checks.push(["second load leaves the pipeline view alone", rerun.jobsmith_pipeline_view === "table"]);

const fail = report(checks);
if (fail) {
  console.error(`\ntest_layout_migration: ${fail} check(s) failed`);
  process.exit(1);
}
console.log("\ntest_layout_migration.js: all checks passed");
