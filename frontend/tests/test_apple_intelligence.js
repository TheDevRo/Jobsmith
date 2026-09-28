// Apple Intelligence (on-device) UI: the Settings opt-ins must appear ONLY on a
// machine the backend says can run the model, and the wizard's zero-setup offer
// wizard's Local card must paint from the real /api/onboarding/status shape and
// put every tier (résumés included) on Apple Intelligence.
//
// Same jsdom style as the other frontend tests: the real index.html plus the
// real settings.js / onboarding.js, eval'd as one unit so their top-level
// function declarations become window globals. api() is stubbed; nothing here
// touches the network.
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const ROOT = path.join(__dirname, "..");
const JS_DIR = path.join(ROOT, "js");
const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
const SENTINEL = "apple-on-device";

function report(checks) {
  let fail = 0;
  for (const [name, ok] of checks) {
    console.log((ok ? "PASS" : "FAIL") + "  " + name);
    if (!ok) fail++;
  }
  return fail;
}

const virtualConsole = new VirtualConsole();
const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost:8888/", virtualConsole });
const { window } = dom;
const doc = window.document;

// Helpers the two files borrow from core.js / review.js, plus the network stub.
let statusPayload = { ok: false, models: [], error: "Connection refused" };
let apiCalls = [];
window.api = (url) => { apiCalls.push(url); return Promise.resolve(url === "/api/ai/status" ? statusPayload : url === "/api/onboarding/status" ? { needs_onboarding: false, tour_complete: true } : {}); };
window.toast = () => {};
window.esc = (s) => String(s == null ? "" : s);
window.splitCsvSmart = (v) => (v || "").split(",").map(s => s.trim()).filter(Boolean);

window.eval(
  ["settings.js", "onboarding.js"].map(f => fs.readFileSync(path.join(JS_DIR, f), "utf8")).join("\n;\n")
  // `let _obState` stays inside the eval's scope; the wizard's own module-level
  // state is what this test needs to reset, so hand it out explicitly.
  + "\n;window._obState = _obState;"
);

const checks = [];
const block = doc.getElementById("ai-ondevice-block");
const warn = doc.getElementById("ai-ondevice-warn");
const cb = t => doc.getElementById("cfg-ai-ondevice-" + t);
const sel = t => doc.getElementById("cfg-ai-model-" + t);

(async () => {
  // ---- 1. Settings: the control is gated on on_device.supported ----
  checks.push(["on-device block starts hidden", block.style.display === "none"]);

  await window.refreshOnDeviceUI({ on_device: { supported: false, available: false, reason: "requires macOS 26" } });
  checks.push(["unsupported machine → block stays hidden", block.style.display === "none"]);

  await window.refreshOnDeviceUI({ on_device: { supported: true, available: true, reason: null } });
  checks.push(["supported + available → block is shown", block.style.display !== "none"]);
  checks.push(["available → no warning", warn.style.display === "none"]);

  await window.refreshOnDeviceUI({ on_device: { supported: true, available: false, reason: "Apple Intelligence is turned off" } });
  checks.push(["supported but off → block shown", block.style.display !== "none"]);
  checks.push(["supported but off → the backend's reason is surfaced verbatim",
    warn.style.display !== "none" && warn.textContent === "Apple Intelligence is turned off"]);

  // A status payload with no on_device field at all (older backend) hides it.
  await window.refreshOnDeviceUI({ ok: true, models: ["m"] });
  checks.push(["status without on_device → hidden", block.style.display === "none"]);

  // ---- 2. Settings: checkbox ⇄ tier model field ----
  sel("fast").innerHTML = '<option value="small-model">small-model</option>';
  sel("strong").innerHTML = '<option value="big-model">big-model</option>';
  sel("utility").innerHTML = '<option value="">—</option>';
  sel("fast").value = "small-model";
  sel("strong").value = "big-model";

  window.applyOnDeviceTiers({ strong: "big-model", fast: SENTINEL, utility: "" });
  checks.push(["saved sentinel ticks that tier's box", cb("fast").checked === true]);
  checks.push(["other tiers stay unticked", cb("strong").checked === false && cb("utility").checked === false]);
  checks.push(["ticked tier's model picker is disabled", sel("fast").disabled === true]);
  checks.push(["untouched tier's picker stays enabled", sel("strong").disabled === false]);

  checks.push(["ticked tier saves the sentinel", window.onDeviceTierModel("fast") === SENTINEL]);
  checks.push(["unticked tier saves its picker value", window.onDeviceTierModel("strong") === "big-model"]);

  cb("fast").checked = false;
  window.applyOnDeviceTier("fast");
  checks.push(["unticking restores the picker's value", window.onDeviceTierModel("fast") === "small-model"]);
  checks.push(["unticking re-enables the picker", sel("fast").disabled === false]);

  cb("strong").checked = true;
  window.applyOnDeviceTier("strong");
  checks.push(["any tier can go on-device, including strong", window.onDeviceTierModel("strong") === SENTINEL]);
  cb("strong").checked = false;
  window.applyOnDeviceTier("strong");

  // ---- 3. Wizard: the Local card paints from the real /api/onboarding/status shape ----
  const realStatus = (on_device) => ({
    needs_onboarding: true, tour_complete: false, profile_ok: false, extension_paired: false,
    ai: { ok: false, models: [], error: "Connection refused" },
    on_device, setup_mode: "", provider: "",
  });
  const local = doc.getElementById("ob-mode-local");
  const reason = doc.getElementById("ob-local-reason");
  const recheck = doc.getElementById("ob-local-recheck");

  window.obApplyAIStatus(realStatus({ supported: false, available: false, reason: "Apple Intelligence requires macOS 26 on Apple Silicon" }));
  checks.push(["unsupported Mac → Local card disabled", local.disabled === true]);
  checks.push(["unsupported Mac → hardware reason shown",
    reason.style.display !== "none" && reason.textContent === "Needs a Mac with Apple silicon on macOS 26+"]);
  checks.push(["unavailable → Check again offered", recheck.style.display !== "none"]);

  window.obApplyAIStatus(realStatus({ supported: true, available: false, reason: "Apple Intelligence is turned off" }));
  checks.push(["turned off → the actionable reason",
    reason.textContent === "Turn on Apple Intelligence in System Settings, then tap Check again"]);

  window.obApplyAIStatus(realStatus({ supported: true, available: true, reason: null }));
  checks.push(["available → Local card enabled, no reason, no Check again",
    local.disabled === false && reason.style.display === "none" && recheck.style.display === "none"]);

  // A status payload without on_device (older backend) disables Local.
  window.obApplyAIStatus({ needs_onboarding: true, ai: { ok: true, models: ["m"] } });
  checks.push(["status without on_device → Local disabled", local.disabled === true]);
  window.obApplyAIStatus(realStatus({ supported: true, available: true, reason: null }));

  // ---- 4. Wizard: choosing Local = Apple Intelligence on every tier ----
  window.obUseAppleIntelligence();
  let ai = window.obCollectAI();
  checks.push(["Local puts all three tiers on-device (strong included)",
    ai.models.strong === SENTINEL && ai.models.fast === SENTINEL && ai.models.utility === SENTINEL]);
  checks.push(["recommended downloads are pre-checked", doc.getElementById("ob-local-downloads").checked === true]);
  checks.push(["checked → Quick match scores, Local match on",
    ai.scoring_tier === "local-match-model" && ai.nli === true && ai.triage === true]);
  checks.push(["Local leaves the server alone", ai.base_url === null && ai.api_key === null && ai.provider === null]);

  doc.getElementById("ob-local-downloads").checked = false;
  ai = window.obCollectAI();
  checks.push(["unchecked → scoring stays on the (Apple) strong tier, Local match untouched",
    ai.scoring_tier === "strong" && ai.nli === null && ai.triage === false]);
  doc.getElementById("ob-local-downloads").checked = true;

  // Losing availability after the pick drops the Local choice.
  window.obApplyAIStatus(realStatus({ supported: true, available: false, reason: "Apple Intelligence is turned off" }));
  checks.push(["Local deselected when it becomes unavailable", window._obState.ai.mode === ""]);

  // Nothing on-device related fetches on its own: the only calls are the
  // wizard's own DOMContentLoaded status check, which jsdom fires for us.
  const ALLOWED = ["/api/onboarding/status", "/api/stats"];
  checks.push(["no unexpected network calls", apiCalls.every(u => ALLOWED.includes(u))]);

  const fails = report(checks);
  if (fails) { console.error(`\ntest_apple_intelligence.js: ${fails} check(s) failed`); process.exit(1); }
  console.log("\ntest_apple_intelligence.js: all checks passed");
})();
