// handshake.js — the Applicant Assist check-in state machine.
//
// ONE implementation, two callers: the localhost content script
// (assist_handshake.js) and the background script, which both need to turn a
// launch page's {sessionId, setupToken} into a working {backendUrl, token} in
// extension storage and a successful POST /api/ext/assist/checkin.
//
// Requires common/storage.js to be loaded first.

(function (root) {
  const Storage = root.JobsmithStorage;

  // Runs the checkin, healing a stale stored token from the page's setup
  // token, then persists whatever worked so the popup/panel talk to the same
  // backend (also heals a moved port).
  //
  // Returns { ok: true, token } or { ok: false, reason, status? }.
  async function assistCheckin({ origin, sessionId, setupToken, log }) {
    const say = log || function () {};

    let stored;
    try {
      stored = await Storage.get(["backendUrl", "token"]);
    } catch (e) {
      say("storage.get failed", e);
      return { ok: false, reason: "storage" };
    }
    stored = stored || {};
    const hasToken = !!stored.token;
    say("stored token present:", hasToken);

    async function checkin(tok) {
      const resp = await fetch(origin + "/api/ext/assist/checkin", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Jobsmith-Token": tok },
        body: JSON.stringify({ session_id: sessionId, had_token: hasToken }),
      });
      say("checkin status:", resp.status);
      return resp;
    }

    let token = hasToken ? stored.token : setupToken;
    try {
      let resp = await checkin(token);
      if (resp.status === 401 && token !== setupToken) {
        // Stored token is stale (backend token rotated, or a different
        // Jobsmith instance). The page's setup token is authoritative for
        // this session.
        say("stored token rejected; retrying with setup token");
        token = setupToken;
        resp = await checkin(token);
      }
      if (!resp.ok) {
        const body = await resp.text().catch(() => "");
        say("checkin failed body:", body);
        return { ok: false, reason: "checkin", status: resp.status };
      }
      // The setup token is ephemeral and per-session — it only exists to prove
      // we're the extension the launch page was opened for. checkin trades it
      // for the persistent token, which is what we must actually store; keeping
      // the setup token would leave us with a credential that stops working.
      const data = await resp.json().catch(() => ({}));
      if (data && data.token) {
        token = data.token;
        say("received persistent token from checkin");
      }
    } catch (e) {
      say("checkin fetch threw:", e);
      return { ok: false, reason: "network" };
    }

    if (stored.token !== token || stored.backendUrl !== origin) {
      try {
        await Storage.set({ backendUrl: origin, token });
        say("persisted backendUrl + token");
      } catch (e) {
        say("storage.set failed", e);
      }
    }
    return { ok: true, token };
  }

  root.JobsmithHandshake = { assistCheckin };
})(typeof globalThis !== "undefined" ? globalThis : this);
