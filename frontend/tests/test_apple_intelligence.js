// Apple Intelligence (on-device) UI: the Settings opt-ins must appear ONLY on a
// machine the backend says can run the model, and the wizard's zero-setup offer
// must appear only when no endpoint answered — while leaving the `strong` tier
// (résumés, cover letters) on the configured endpoint.
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
window.api = (url) => { apiCalls.push(url); return Promise.resolve(url === "/api/ai/status" ? statusPayload : {}); };
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

  // ---- 3. Wizard: the offer only appears when nothing else answered ----
  const offer = doc.getElementById("ob-ai-ondevice");
  const obSel = t => doc.getElementById("ob-ai-model-" + t);

  window.obApplyAIStatus({ ok: false, models: [], error: "Connection refused",
                           on_device: { supported: true, available: false, reason: "off" } });
  checks.push(["no endpoint + on-device unavailable → no offer", offer.style.display === "none"]);

  window.obApplyAIStatus({ ok: true, models: ["big-model"],
                           on_device: { supported: true, available: true, reason: null } });
  checks.push(["endpoint answered → no offer (the real model wins)", offer.style.display === "none"]);

  window.obApplyAIStatus({ ok: false, models: [], error: "Connection refused",
                           on_device: { supported: true, available: true, reason: null } });
  checks.push(["no endpoint + on-device available → offer shown", offer.style.display !== "none"]);

  // A status whose only model is the sentinel is still "no endpoint answered".
  window.obApplyAIStatus({ ok: true, models: [SENTINEL],
                           on_device: { supported: true, available: true, reason: null } });
  checks.push(["sentinel-only model list still counts as no endpoint", offer.style.display !== "none"]);

  // ---- 4. Wizard: accepting the offer ----
  obSel("strong").innerHTML = '<option value="">—</option>';
  window.obUseAppleIntelligence();
  checks.push(["fast tier moves on-device", obSel("fast").value === SENTINEL]);
  checks.push(["utility tier moves on-device", obSel("utility").value === SENTINEL]);
  checks.push(["strong (résumés/cover letters) is left alone", obSel("strong").value !== SENTINEL]);
  checks.push(["the offer is dismissed once taken", offer.style.display === "none"]);

  const payload = window.obBuildPayload();
  checks.push(["payload routes scoring to the on-device tier", payload.ai.scoring_tier === "fast"]);
  checks.push(["payload writes the sentinel for fast + utility",
    payload.ai.models.fast.model === SENTINEL && payload.ai.models.utility.model === SENTINEL]);
  checks.push(["payload leaves strong on the endpoint", payload.ai.models.strong.model !== SENTINEL]);

  // Without the offer taken, the wizard must not touch scoring_tier at all.
  window._obState.onDevice = false;
  checks.push(["untaken offer leaves scoring_tier unset",
    window.obBuildPayload().ai.scoring_tier === undefined]);

  // Nothing on-device related fetches on its own: the only calls are the
  // wizard's own DOMContentLoaded status check, which jsdom fires for us.
  const ALLOWED = ["/api/onboarding/status", "/api/stats"];
  checks.push(["no unexpected network calls", apiCalls.every(u => ALLOWED.includes(u))]);

  const fails = report(checks);
  if (fails) { console.error(`\ntest_apple_intelligence.js: ${fails} check(s) failed`); process.exit(1); }
  console.log("\ntest_apple_intelligence.js: all checks passed");
})();
