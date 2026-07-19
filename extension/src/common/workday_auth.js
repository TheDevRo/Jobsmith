// workday_auth.js — one-tap Workday account-creation / sign-in for the assisted
// surfaces (extension side panel + iOS Apply browser). Registers two globals in
// the isolated world:
//
//   window.__jobsmithWorkdayAuthState()          -> { state, tenantHost }
//   window.__jobsmithWorkdayAuth(email, password) -> Promise<{ ok, action, message, pending }>
//
// The side panel / Apply browser injects this file once, then calls the globals
// via a second scripting.executeScript({func,args}) (extension) or
// callAsyncJavaScript (iOS). It is SELF-CONTAINED — it does NOT depend on
// fill.js being loaded first, so the small React native-setter helper is
// duplicated here (Workday is React; a plain `.value =` snaps back on render).
//
// SINGLE SOURCE: extension/src/common/workday_auth.js is the original. A
// verbatim copy is bundled for iOS at ios-standalone/App/Apply/JS/workday_auth.js.
// Keep the two in sync when either changes.
//
// The password is never stored, logged, or persisted — it lives only as a call
// argument for the duration of one submit.

(function () {
  "use strict";

  // Workday career sites live on both suffixes (wd5.myworkdayjobs.com and
  // wd1.myworkdaysite.com style tenants).
  const WORKDAY_HOST_SUFFIXES = ["myworkdayjobs.com", "myworkdaysite.com"];
  const SEL = {
    email: "input[data-automation-id='email'], input[type='email']",
    password: "input[data-automation-id='password'], input[type='password']",
    verifyPassword: "input[data-automation-id='verifyPassword']",
    createSubmit: "[data-automation-id='createAccountSubmitButton']",
    signInSubmit: "[data-automation-id='signInSubmitButton']",
    createLink: "[data-automation-id='createAccountLink']",
    legalCheckbox: "input[data-automation-id='createAccountCheckbox']",
    // Workday surfaces validation/auth failures in an error banner. Cover the
    // adapter's automation-id plus generic alert roles as a fallback.
    error: "[data-automation-id='errorMessage'], [data-automation-id='alert'], [role='alert']",
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function onWorkdayHost() {
    const host = (location.hostname || "").toLowerCase();
    return WORKDAY_HOST_SUFFIXES.some((s) => host.endsWith(s));
  }

  function q(sel) {
    try { return document.querySelector(sel); } catch (_) { return null; }
  }

  function isVisible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  // React tracks the last value it knows about on el._valueTracker. Seeding the
  // tracker with the current value forces React to treat the upcoming setter as
  // a real user change, so onChange fires and the value sticks instead of
  // snapping back on the next render. (Duplicated from fill.js by design — this
  // script must stand alone.)
  function nativeSet(el, value) {
    const proto =
      el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype :
      el instanceof HTMLSelectElement ? HTMLSelectElement.prototype :
      HTMLInputElement.prototype;
    const tracker = el._valueTracker;
    if (tracker && typeof tracker.setValue === "function") {
      try { tracker.setValue(el.value || ""); } catch (_) { /* ignore */ }
    }
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
  }

  function fireInputEvents(el) {
    el.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    try {
      el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText" }));
    } catch (_) {
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
  }

  function setInput(el, value) {
    if (!el) return false;
    try { el.focus(); } catch (_) {}
    nativeSet(el, value);
    fireInputEvents(el);
    return true;
  }

  // A create-account screen is uniquely marked by the verifyPassword field —
  // it is absent on the sign-in screen and on ordinary form pages. Sign-in is
  // email+password with no verifyPassword. Anything else is "none" (already
  // authenticated, or not an auth page).
  function detectState() {
    if (!onWorkdayHost()) return "none";
    const verify = q(SEL.verifyPassword);
    const email = q(SEL.email);
    const password = q(SEL.password);
    if (isVisible(verify)) return "create";
    if (isVisible(email) && isVisible(password)) return "signin";
    return "none";
  }

  function authFormPresent() {
    return isVisible(q(SEL.verifyPassword)) ||
      (isVisible(q(SEL.email)) && isVisible(q(SEL.password)));
  }

  function errorText() {
    let el = null;
    try { el = document.querySelector(SEL.error); } catch (_) { return ""; }
    if (!isVisible(el)) return "";
    return (el.textContent || "").trim();
  }

  // After a successful create, Workday commonly shows an email-verification
  // interstitial ("check your email to verify your account"). Detect it so the
  // caller can record status="pending_verification".
  function verificationPending() {
    const marker = q("[data-automation-id='verifyEmailPage'], [data-automation-id='emailVerification']");
    if (isVisible(marker)) return true;
    const bodyText = (document.body && document.body.innerText || "").toLowerCase();
    return /verify your email|check your (email|inbox)|verification (email|link)/.test(bodyText);
  }

  window.__jobsmithWorkdayAuthState = function jobsmithWorkdayAuthState() {
    return { state: detectState(), tenantHost: (location.hostname || "").toLowerCase() };
  };

  window.__jobsmithWorkdayAuth = async function jobsmithWorkdayAuth(email, password) {
    if (!onWorkdayHost()) {
      return { ok: false, action: "error", message: "Not a Workday host" };
    }
    const state = detectState();
    if (state === "none") {
      return { ok: false, action: "error", message: "No Workday sign-in form on this page" };
    }
    if (!email || !password) {
      return { ok: false, action: "error", message: "Missing Workday credentials" };
    }

    const creating = state === "create";
    try {
      setInput(q(SEL.email), email);
      await sleep(40);
      setInput(q(SEL.password), password);
      if (creating) {
        await sleep(40);
        setInput(q(SEL.verifyPassword), password);
        // Accept the legal/terms checkbox if the tenant shows one.
        const legal = q(SEL.legalCheckbox);
        if (legal && !legal.checked) {
          try { legal.click(); } catch (_) {}
          if (!legal.checked) {
            legal.checked = true;
            legal.dispatchEvent(new Event("input", { bubbles: true }));
            legal.dispatchEvent(new Event("change", { bubbles: true }));
          }
        }
      }
      await sleep(60);

      const submit = q(creating ? SEL.createSubmit : SEL.signInSubmit);
      if (!isVisible(submit)) {
        return { ok: false, action: "error", message: "Sign-in button not found" };
      }
      try { submit.click(); } catch (_) {}

      // Poll for the outcome: success when the auth form unmounts / the page
      // navigates; failure when an error banner appears. ~8s budget.
      const deadline = Date.now() + 8000;
      while (Date.now() < deadline) {
        await sleep(250);
        const err = errorText();
        if (err) {
          return { ok: false, action: "error", message: err.slice(0, 200) };
        }
        if (!authFormPresent()) {
          if (creating) {
            const pending = verificationPending();
            return {
              ok: true,
              action: "account_created",
              pending,
              message: pending
                ? "Account created — check your email to verify it."
                : "Account created.",
            };
          }
          return { ok: true, action: "signed_in", pending: false, message: "Signed in." };
        }
      }
      // Timed out with the form still up and no banner — treat as failure.
      const err = errorText();
      return {
        ok: false,
        action: "error",
        message: err ? err.slice(0, 200) : "Timed out waiting for Workday to respond",
      };
    } catch (e) {
      return { ok: false, action: "error", message: String((e && e.message) || e) };
    }
  };
})();

// Final expression must be structured-clonable for Firefox's executeScript.
true;
