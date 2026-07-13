// storage.js — promise-shimmed extension storage, shared by every context
// (background worker, content scripts, popup, panel).
//
// Firefox MV3 returns Promises from storage.*.get/set and ignores any callback
// arg. Chrome MV3 supports both. Use the call's return value and only fall
// back to the callback form when it isn't thenable (older Chrome).

(function (root) {
  const ns = (typeof browser !== "undefined") ? browser : chrome;

  function call(areaName, method, arg) {
    const area = ns.storage && ns.storage[areaName];
    if (!area) return Promise.reject(new Error(`storage.${areaName} unavailable`));
    const r = area[method](arg);
    if (r && typeof r.then === "function") return r;
    return new Promise((resolve) => area[method](arg, resolve));
  }

  // storage.session survives an MV3 service-worker restart but never hits
  // disk. Where it's missing (older Firefox), fall back to local — losing the
  // privacy niceness is better than losing the state.
  function sessionArea() {
    return (ns.storage && ns.storage.session) ? "session" : "local";
  }

  root.JobsmithStorage = {
    get: (keys) => call("local", "get", keys).then((o) => o || {}),
    set: (values) => call("local", "set", values),
    sessionGet: (keys) => call(sessionArea(), "get", keys).then((o) => o || {}),
    sessionSet: (values) => call(sessionArea(), "set", values),
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
