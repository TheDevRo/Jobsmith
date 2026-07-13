// permissions.js — optional host-permission helpers.
//
// The manifest only *requires* the origins the core flow can't work without:
// the local backend (localhost / 127.0.0.1) plus LinkedIn and Indeed (whose
// cookies the popup syncs). Everything else — the long tail of ATS domains —
// lives in optional_host_permissions and is granted on demand, from a user
// gesture, by the popup. Chrome and Firefox both require that gesture, so the
// background can never call request() itself: it flags the tab instead and the
// user grants from the toolbar popup.

(function (root) {
  const ns = (typeof browser !== "undefined") ? browser : chrome;

  // Mirror of manifest.host_permissions — origins we hold unconditionally.
  const ALWAYS = [
    /^https?:\/\/(?:localhost|127\.0\.0\.1)(?::\d+)?\//i,
    /^https:\/\/(?:[a-z0-9-]+\.)*linkedin\.com\//i,
    /^https:\/\/(?:[a-z0-9-]+\.)*indeed\.com\//i,
  ];

  function originPattern(url) {
    try {
      const u = new URL(url);
      if (u.protocol !== "http:" && u.protocol !== "https:") return null;
      return `${u.protocol}//${u.hostname}/*`;
    } catch (_) {
      return null;
    }
  }

  function isAlwaysGranted(url) {
    return ALWAYS.some((re) => re.test(url || ""));
  }

  function promisify(method, arg) {
    const r = ns.permissions[method](arg);
    if (r && typeof r.then === "function") return r;
    return new Promise((resolve) => ns.permissions[method](arg, resolve));
  }

  const available = () => !!(ns.permissions && ns.permissions.contains);

  // True when we may script `url`. When the permissions API isn't reachable
  // (Firefox gives an extension page iframed in a web page only
  // content-script-level privileges) we optimistically return true and let the
  // executeScript call be the judge — a false "no access" banner would be
  // worse than a real error message.
  async function hasSiteAccess(url) {
    if (isAlwaysGranted(url)) return true;
    const pattern = originPattern(url);
    if (!pattern) return false;
    if (!available()) return true;
    try {
      return !!(await promisify("contains", { origins: [pattern] }));
    } catch (_) {
      return true;
    }
  }

  // Must be called from a user-gesture handler (a click in the popup).
  async function requestSiteAccess(url) {
    const pattern = originPattern(url);
    if (!pattern) return false;
    if (!available()) return false;
    try {
      return !!(await promisify("request", { origins: [pattern] }));
    } catch (_) {
      return false;
    }
  }

  async function hasAllSitesAccess() {
    if (!available()) return true;
    try {
      return !!(await promisify("contains", { origins: ["<all_urls>"] }));
    } catch (_) {
      return false;
    }
  }

  async function requestAllSitesAccess() {
    if (!available()) return false;
    try {
      return !!(await promisify("request", { origins: ["<all_urls>"] }));
    } catch (_) {
      return false;
    }
  }

  root.JobsmithPermissions = {
    originPattern,
    isAlwaysGranted,
    hasSiteAccess,
    requestSiteAccess,
    hasAllSitesAccess,
    requestAllSitesAccess,
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
