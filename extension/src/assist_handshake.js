// assist_handshake.js — runs on http(s)://(localhost|127.0.0.1)/assist/launch/*
// (any port — the desktop backend may bind a random one when 8888 is taken).
// Reads the session record embedded in the page, then hands off to the shared
// check-in state machine (common/handshake.js, also used by the background),
// which auto-provisions the backend token into extension storage if needed or
// stale and calls /api/ext/assist/checkin so the page can detect the extension
// is present and redirect to the job.

(function () {
  const TAG = "[Jobsmith handshake]";
  const log = (...a) => console.log(TAG, ...a);
  log("content script loaded at", window.location.href);

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
  const Handshake = globalThis.JobsmithHandshake;
  if (!Handshake) {
    console.error(TAG, "common/handshake.js did not load");
    return;
  }

  const el = document.getElementById("jobsmith-session");
  if (!el) {
    console.warn(TAG, "no #jobsmith-session element on page; aborting");
    return;
  }

  const sessionId = el.getAttribute("data-session") || "";
  const setupToken = el.getAttribute("data-setup-token") || "";
  log("session=", sessionId, "hasSetupToken=", !!setupToken);
  if (!sessionId || !setupToken) {
    console.warn(TAG, "missing session/setup attributes; aborting");
    return;
  }

  Handshake.assistCheckin({
    origin: window.location.origin,
    sessionId,
    setupToken,
    log,
  });
})();
