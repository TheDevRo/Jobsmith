// Setup Assistant step 0 (Local / Cloud / Advanced) in the desktop wizard.
//
// Same jsdom style as the other frontend tests: the real index.html plus the
// real settings.js / onboarding.js, eval'd as one unit. api() is a routed stub
// that records every call; nothing here touches the network.
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const ROOT = path.join(__dirname, "..");
const JS_DIR = path.join(ROOT, "js");
const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
const PROVIDERS = JSON.parse(fs.readFileSync(path.join(ROOT, "..", "backend", "ai_providers.json"), "utf8"));
const SENTINEL = "apple-on-device";

function report(checks) {
  let fail = 0;
  for (const [name, ok] of checks) {
    console.log((ok ? "PASS" : "FAIL") + "  " + name);
    if (!ok) fail++;
  }
  return fail;
}

const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost:8888/", virtualConsole: new VirtualConsole() });
const { window } = dom;
const doc = window.document;

// ---- routed api() stub ----
let calls = [];
let cfg = {};
let modelsReply = { ok: true, models: [], message: "", detail: "" };
let chatReply = { ok: true, code: "ok", message: "", detail: "" };
const status = (on_device, extra = {}) => ({
  needs_onboarding: true, tour_complete: false, profile_ok: false, extension_paired: false,
  ai: { ok: false, models: [], error: "offline" }, on_device, setup_mode: "", provider: "", ...extra,
});
let statusReply = status({ supported: true, available: true, reason: null });
window.api = (url, opts = {}) => {
  calls.push({ url, method: opts.method || "GET", body: opts.body ? JSON.parse(opts.body) : null });
  const r = {
    "/api/onboarding/status": statusReply,
    "/api/ai/providers": PROVIDERS,
    "/api/config": opts.method === "POST" ? { message: "ok" } : cfg,
    "/api/ai/triage/status": { state: "not_installed", size_bytes: 134e6, progress: 0 },
    "/api/ai/nli/status": { state: "off", size_bytes: 690e6, progress: 0 },
    "/api/ai/models": modelsReply,
    "/api/ai/test-chat": chatReply,
    "/api/onboarding/ai": { saved: true },
  }[url];
  return Promise.resolve(r === undefined ? {} : r);
};
window.toast = () => {};
window.esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
window.splitCsvSmart = (v) => (v || "").split(",").map(s => s.trim()).filter(Boolean);
window.appConfirm = () => Promise.resolve(true);

window.eval(
  ["settings.js", "onboarding.js"].map(f => fs.readFileSync(path.join(JS_DIR, f), "utf8")).join("\n;\n")
  + "\n;window._obState = _obState;"
);

const $ = id => doc.getElementById(id);
const tick = async (n = 6) => { for (let i = 0; i < n; i++) await new Promise(r => setTimeout(r, 0)); };
const posts = url => calls.filter(c => c.url === url && c.method === "POST");
const optionValues = sel => [...sel.options].map(o => o.value);
const checks = [];

async function open(opts) {
  calls = [];
  window.obOpen(opts);
  await tick();
}

(async () => {
  await tick();

  // ---- 1. the three cards ----
  await open({ status: statusReply });
  const cards = [...doc.querySelectorAll(".ob-mode-card")].map(c => c.querySelector(".ob-mode-title").textContent);
  checks.push(["three cards: Local, Cloud, Advanced", JSON.stringify(cards) === '["Local","Cloud","Advanced"]']);
  checks.push(["header copy", doc.querySelector('[data-panel="0"] h2').textContent === "How should Jobsmith think?"]);
  checks.push(["stepper label is AI", doc.querySelector('.ob-step[data-step="0"] .ob-step-label').textContent === "AI"]);
  checks.push(["nothing picked on open", window._obState.ai.mode === "" && !doc.querySelector(".ob-mode-card.selected")]);
  checks.push(["Next reads Continue on step 0", $("ob-next").textContent === "Continue →"]);
  checks.push(["download size computed from the backend (134 + 690)", $("ob-local-size").textContent === "824"
    && $("ob-triage-size").textContent === "134"]);
  checks.push(["opening the wizard writes nothing", posts("/api/config").length === 0 && posts("/api/onboarding/ai").length === 0]);

  window.obSelectMode("cloud");
  checks.push(["picking a card shows only its panel", $("ob-sub-cloud").style.display === ""
    && $("ob-sub-local").style.display === "none" && $("ob-sub-advanced").style.display === "none"]);
  checks.push(["picked card is marked", $("ob-mode-cloud").classList.contains("selected")
    && $("ob-mode-cloud").getAttribute("aria-checked") === "true"]);

  // ---- 2. Local disabled reason ----
  window.obApplyAIStatus(status({ supported: true, available: false, reason: "Apple Intelligence is not enabled" }));
  checks.push(["Local disabled with the real reason", $("ob-mode-local").disabled
    && $("ob-local-reason").textContent === "Turn on Apple Intelligence in System Settings, then tap Check again"]);
  window.obSelectMode("local");
  checks.push(["a disabled Local card cannot be picked", window._obState.ai.mode === "cloud"]);
  statusReply = status({ supported: true, available: true, reason: null });
  calls = [];
  window.obCheckLocal();
  await tick();
  checks.push(["Check again re-reads the status and enables Local",
    calls.some(c => c.url === "/api/onboarding/status") && $("ob-mode-local").disabled === false]);

  // ---- 3. Cloud preset: filled, read-only URL; key required; key link ----
  window.obSelectMode("cloud");
  const provSel = $("ob-cloud-provider");
  checks.push(["provider list = presets then Custom last",
    JSON.stringify(optionValues(provSel)) === JSON.stringify(["", ...PROVIDERS.map(p => p.name), "custom"])]);
  provSel.value = "OpenRouter";
  window.obSelectProvider("OpenRouter");
  await tick();
  checks.push(["preset fills the URL", $("ob-cloud-url").value === "https://openrouter.ai/api/v1"]);
  checks.push(["preset URL is read-only", $("ob-cloud-url").readOnly === true]);
  checks.push(["Edit address + Get an API key shown", $("ob-cloud-edit-url").style.display === ""
    && $("ob-cloud-key-link").style.display === "" && $("ob-cloud-key-link").href === "https://openrouter.ai/keys"]);
  checks.push(["preset key is not marked optional", $("ob-cloud-key-opt").style.display === "none"]);
  checks.push(["no model listing before a key", !calls.some(c => c.url === "/api/ai/models")]);

  // Gemini's path is not /v1 — presets never warn.
  provSel.value = "Google Gemini";
  window.obSelectProvider("Google Gemini");
  checks.push(["presets never show the /v1 warning", $("ob-cloud-url-warn").style.display === "none"]);

  // key entered → searchable list, non-chat filtered, no auto-pick
  provSel.value = "OpenRouter";
  window.obSelectProvider("OpenRouter");
  modelsReply = { ok: true, models: ["z-ai/glm:free", "meta/llama-3:free", "openai/text-embedding-3-small",
                                     "openai/whisper-1", "openai/dall-e-3", "x/rerank-v2"], message: "", detail: "" };
  $("ob-cloud-key").value = "sk-or-123";
  await window.obListModels();
  const listCall = calls.filter(c => c.url === "/api/ai/models").pop();
  checks.push(["listing sends the typed URL + key", listCall && listCall.body.base_url === "https://openrouter.ai/api/v1"
    && listCall.body.api_key === "sk-or-123"]);
  checks.push(["non-chat ids hidden by default", JSON.stringify(optionValues($("ob-model-list"))) === '["z-ai/glm:free","meta/llama-3:free"]']);
  checks.push(["no auto-pick: nothing selected", $("ob-model-list").value === "" && window.obCollectAI().models.strong === ""]);
  $("ob-model-showall").checked = true;
  window.obRenderModelList();
  checks.push(["Show all brings them back", $("ob-model-list").options.length === 6]);
  $("ob-model-showall").checked = false;
  $("ob-model-search").value = "llama";
  window.obRenderModelList();
  checks.push(["search filters the list", JSON.stringify(optionValues($("ob-model-list"))) === '["meta/llama-3:free"]']);
  window.obPickWriting("meta/llama-3:free");
  let ai = window.obCollectAI();
  checks.push(["Writing model fills all tiers by default", ai.models.strong === "meta/llama-3:free"
    && ai.models.fast === "meta/llama-3:free" && ai.models.utility === "meta/llama-3:free" && ai.scoring_tier === "strong"]);
  checks.push(["Quick match download is unchecked for Cloud", $("ob-cloud-triage").checked === false && ai.triage === false]);
  checks.push(["scoring disclosure starts collapsed", $("ob-cloud-more").open === false]);
  $("ob-cloud-scoring").value = "z-ai/glm:free";
  ai = window.obCollectAI();
  checks.push(["a separate scoring model moves scoring to that tier", ai.models.fast === "z-ai/glm:free" && ai.scoring_tier === "fast"]);
  $("ob-cloud-scoring").value = "";

  // "Edit address" switches to Custom and keeps the URL
  window.obEditAddress();
  checks.push(["Edit address → Custom with the URL kept", provSel.value === "custom"
    && $("ob-cloud-url").value === "https://openrouter.ai/api/v1" && $("ob-cloud-url").readOnly === false]);

  // ---- 4. Custom with an empty key; free-text fallback ----
  provSel.value = "custom";
  window.obSelectProvider("custom");
  checks.push(["Custom starts empty, key optional", $("ob-cloud-url").value === "" && $("ob-cloud-key-opt").style.display === ""]);
  modelsReply = { ok: false, models: [], message: "Could not reach the server at rig.local", detail: "ConnectError" };
  calls = [];
  $("ob-cloud-url").value = "rig.local/v1/chat/completions/";
  window.obCloudUrlChanged();
  await tick();
  checks.push(["Custom URL normalised (scheme added, /chat/completions stripped)", $("ob-cloud-url").value === "https://rig.local/v1"]);
  const customList = calls.find(c => c.url === "/api/ai/models");
  checks.push(["Custom lists at once with an empty key", customList && customList.body.api_key === ""]);
  checks.push(["listing failed → free-text Model ID with the plain-English error", $("ob-model-free").style.display === ""
    && $("ob-model-picker").style.display === "none" && $("ob-model-error").textContent.startsWith("Could not reach the server at rig.local")]);
  $("ob-cloud-url").value = "http://192.0.2.5:1234";
  window.obCloudUrlChanged();
  checks.push(["Custom without /v1 warns (does not block)", $("ob-cloud-url-warn").style.display === ""]);
  modelsReply = { ok: true, models: [], message: "", detail: "" };
  await window.obListModels();
  checks.push(["empty list → free-text fallback too", $("ob-model-free").style.display === ""]);
  $("ob-cloud-url").value = "https://lmstudio.example/v1";
  window.obCloudUrlChanged();
  await tick();
  $("ob-model-id").value = "qwen3";
  window.obPickWriting("qwen3");

  // ---- 5. shared exit: tests, posts once, before the Résumé step ----
  calls = [];
  chatReply = { ok: false, code: "auth", message: "That API key was rejected", detail: "AuthenticationError: 401" };
  await window.obAIContinue();
  checks.push(["failed test stays on step 0", window._obState.step === 0]);
  checks.push(["failed test shows the plain-English message", $("ob-ai-status").textContent === "That API key was rejected"]);
  checks.push(["Show details + Continue anyway offered", $("ob-ai-details-btn").style.display === ""
    && $("ob-ai-details").textContent.includes("401") && $("ob-ai-fail-actions").style.display === ""]);
  checks.push(["a failed test saves nothing", posts("/api/onboarding/ai").length === 0 && posts("/api/config").length === 0]);

  calls = [];
  chatReply = { ok: true, code: "ok", message: "", detail: "" };
  const stepAtPost = [];
  const realApi = window.api;
  window.api = (url, opts) => { if (url === "/api/onboarding/ai") stepAtPost.push(window._obState.step); return realApi(url, opts); };
  await window.obAIContinue();
  window.api = realApi;
  const chat = posts("/api/ai/test-chat");
  checks.push(["Continue pings the Writing model with the typed values", chat.length === 1
    && chat[0].body.model === "qwen3" && chat[0].body.base_url === "https://lmstudio.example/v1" && chat[0].body.api_key === ""]);
  const saves = posts("/api/onboarding/ai");
  checks.push(["shared exit posts exactly once", saves.length === 1]);
  checks.push(["…before the Résumé step", stepAtPost.length === 1 && stepAtPost[0] === 0 && window._obState.step === 1]);
  checks.push(["saved payload: cloud/custom, empty key, all tiers", saves[0].body.mode === "cloud"
    && saves[0].body.provider === "custom" && saves[0].body.api_key === "" && saves[0].body.verified === true
    && saves[0].body.models.utility === "qwen3"]);
  checks.push(["no /api/config write at the shared exit", posts("/api/config").length === 0]);
  const finishPayload = window.obBuildPayload();
  checks.push(["first-run Finish payload carries no AI section (already saved)", finishPayload.ai === undefined]);

  // Local path saves the on-device snapshot; scoring tier derives from the mode at save time.
  await open({ status: status({ supported: true, available: true, reason: null }) });
  window.obSelectMode("local");
  calls = [];
  await window.obAIContinue();
  const localSave = posts("/api/onboarding/ai")[0];
  checks.push(["Local pings the sentinel with no server", posts("/api/ai/test-chat")[0].body.model === SENTINEL
    && posts("/api/ai/test-chat")[0].body.base_url === ""]);
  checks.push(["Local saves Apple everywhere + downloads", localSave && localSave.body.mode === "local"
    && localSave.body.models.strong === SENTINEL && localSave.body.scoring_tier === "local-match-model"
    && localSave.body.nli === true && localSave.body.triage === true && localSave.body.base_url === null]);

  // Continue anyway saves, unverified; Set up AI later saves nothing.
  await open({ status: statusReply });
  window.obSelectMode("advanced");
  checks.push(["Advanced shows the full form with a preset shortcut", $("ob-sub-advanced").style.display === ""
    && optionValues($("ob-adv-provider")).includes("OpenAI")]);
  window.obApplyAIStatus(statusReply);
  modelsReply = { ok: true, models: ["big", "small"], message: "", detail: "" };
  $("ob-adv-provider").value = "OpenAI";
  window.obAdvProvider("OpenAI");
  await window.obTestAI();
  checks.push(["Advanced lists Apple + models with no auto-pick", optionValues($("ob-ai-model-strong")).join() === ",apple-on-device,big,small"
    && $("ob-ai-model-strong").value === ""]);
  calls = [];
  await window.obAIContinue();
  checks.push(["empty Writing tier is flagged, nothing sent", $("ob-ai-status").textContent.includes("Writing")
    && calls.length === 0]);
  $("ob-ai-model-strong").value = "big";
  $("ob-adv-scoring").value = "local-match-model";
  chatReply = { ok: false, code: "rate_limit", message: "The provider is rate-limiting; try again in a minute", detail: "429" };
  await window.obAIContinue();
  calls = [];
  window.obContinueAnyway();
  await tick();
  const anyway = posts("/api/onboarding/ai")[0];
  checks.push(["Continue anyway saves, marked unverified, no second ping", anyway && anyway.body.verified === false
    && posts("/api/ai/test-chat").length === 0 && window._obState.step === 1]);
  checks.push(["Advanced payload: preset name kept, Quick match picked → triage", anyway.body.provider === "OpenAI"
    && anyway.body.base_url === "https://api.openai.com/v1" && anyway.body.triage === true]);

  await open({ status: statusReply });
  calls = [];
  window.obSetupLater();
  checks.push(["Set up AI later saves nothing, moves on", calls.length === 0 && window._obState.step === 1]);

  // ---- 6. re-run: never posts config before the diff ----
  cfg = { profile: { full_name: "Real Person", email: "real@example.com", experience: [], education: [] },
          ai: { base_url: "http://old/v1", api_key: "k", provider: "custom", models: { strong: { model: "old" } }, scoring_tier: "strong" },
          search: {}, api_keys: {} };
  await open({ status: status({ supported: true, available: true, reason: null }, { needs_onboarding: false, setup_mode: "cloud", provider: "custom" }) });
  checks.push(["re-run detected", window._obState.rerun === true]);
  checks.push(["re-run re-selects the saved mode + provider", window._obState.ai.mode === "cloud"
    && $("ob-cloud-provider").value === "custom" && $("ob-cloud-url").value === "http://old/v1"]);
  window.obSelectMode("local");
  chatReply = { ok: true, code: "ok", message: "", detail: "" };
  calls = [];
  await window.obAIContinue();
  checks.push(["re-run Continue tests but writes nothing", posts("/api/ai/test-chat").length === 1
    && posts("/api/onboarding/ai").length === 0 && posts("/api/config").length === 0 && window._obState.step === 1]);
  $("ob-name").value = "Real Person";
  $("ob-email").value = "real@example.com";
  await window.obShowDiff();
  checks.push(["still nothing written when the diff opens", posts("/api/config").length === 0 && posts("/api/onboarding/ai").length === 0]);
  const labels = window._obState.diff.map(r => r.field.label);
  checks.push(["diff lists the AI change", labels.includes("AI models") && labels.includes("AI scoring tier")
    && !labels.includes("AI server URL")]);
  await window.obApplyDiff();
  const applied = posts("/api/config")[0];
  checks.push(["apply writes the AI rows through /api/config", applied && applied.body.ai.models.strong.model === SENTINEL]);
  checks.push(["…then records the mode", posts("/api/onboarding/ai").length === 1 && posts("/api/onboarding/ai")[0].body.mode === "local"]);

  // ---- 7. Change setup… (Settings → AI): step 0 only, saves directly ----
  checks.push(["Settings → AI has Change setup…", !!$("ai-change-setup") && $("ai-change-setup").textContent.includes("Change setup")]);
  calls = [];
  window.obChangeSetup();
  await tick();
  checks.push(["Change setup opens step 0 without the stepper", window._obState.only === "ai" && $("ob-stepper").style.display === "none"]);
  window.obSelectMode("local");
  await window.obAIContinue();
  checks.push(["Change setup saves and closes", posts("/api/onboarding/ai").length === 1
    && $("onboarding-overlay").style.display === "none"]);

  const fails = report(checks);
  if (fails) { console.error(`\ntest_setup_modes.js: ${fails} check(s) failed`); process.exit(1); }
  console.log("\ntest_setup_modes.js: all checks passed");
})().catch(e => { console.error(e); process.exit(1); });
