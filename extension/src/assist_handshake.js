// assist_handshake.js — runs on http(s)://(localhost|127.0.0.1):8888/assist/launch/*
// Reads the session record embedded in the page, auto-provisions the backend
// token into extension storage if needed, then calls /api/ext/assist/checkin
// so the page can detect the extension is present and redirect to the job.

(function () {
  const TAG = "[Jobsmith handshake]";
  console.log(TAG, "content script loaded at", window.location.href);

  // Drop a breadcrumb the page itself can see, so even without DevTools we
  // can tell the script ran.
  try {
    const beacon = document.createElement("meta");
    beacon.setAttribute("name", "jobsmith-handshake");
    beacon.setAttribute("content", "loaded");
    document.head && document.head.appendChild(beacon);
  } catch (e) {}

  const ns = (typeof browser !== "undefined") ? browser : chrome;
  if (!ns || !ns.storage || !ns.storage.local) {
    console.error(TAG, "no extension storage API available");
    return;
  }
  const el = document.getElementById("jobsmith-session");
  if (!el) {
    console.warn(TAG, "no #jobsmith-session element on page; aborting");
    return;
  }

  const sessionId = el.getAttribute("data-session") || "";
  const setupToken = el.getAttribute("data-setup-token") || "";
  console.log(TAG, "session=", sessionId, "hasSetupToken=", !!setupToken);
  if (!sessionId || !setupToken) {
    console.warn(TAG, "missing session/setup attributes; aborting");
    return;
  }

  const backendUrl = window.location.origin;

  // Firefox MV3 returns Promises from storage.local.get/set and ignores any
  // callback arg. Chrome MV3 supports both. Use the call's return value and
  // only fall back to the callback form if it isn't thenable (older Chrome).
  function storageGet(keys) {
    const r = ns.storage.local.get(keys);
    if (r && typeof r.then === "function") return r;
    return new Promise((resolve) => ns.storage.local.get(keys, resolve));
  }
  function storageSet(values) {
    const r = ns.storage.local.set(values);
    if (r && typeof r.then === "function") return r;
    return new Promise((resolve) => ns.storage.local.set(values, resolve));
  }

  (async () => {
    let stored;
    try {
      stored = await storageGet(["backendUrl", "token"]);
    } catch (e) {
      console.error(TAG, "storage.get failed", e);
      return;
    }
    const hasToken = !!(stored && stored.token);
    const token = hasToken ? stored.token : setupToken;
    console.log(TAG, "stored token present:", hasToken);

    if (!hasToken || !stored.backendUrl) {
      try {
        await storageSet({
          backendUrl: stored.backendUrl || backendUrl,
          token,
        });
        console.log(TAG, "persisted backendUrl + token into extension storage");
      } catch (e) {
        console.error(TAG, "storage.set failed", e);
      }
    }

    try {
      const resp = await fetch(backendUrl + "/api/ext/assist/checkin", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Jobsmith-Token": token,
        },
        body: JSON.stringify({ session_id: sessionId, had_token: hasToken }),
      });
      console.log(TAG, "checkin status:", resp.status);
      if (!resp.ok) {
        const body = await resp.text().catch(() => "");
        console.error(TAG, "checkin failed body:", body);
      }
    } catch (e) {
      console.error(TAG, "checkin fetch threw:", e);
    }
  })();
})();
