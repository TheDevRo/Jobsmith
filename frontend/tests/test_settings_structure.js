// Phase 3 structural test for Settings: the 9→5 tab regroup was a pure DOM
// move, so the two things that can silently break are (a) the set of cfg-*
// input ids saveSettings() collects — a dropped or duplicated id writes a
// different config.yaml — and (b) the deep links (palette, tour, checklist
// banners) that address panes by id.
//
// Everything here is derived from the real frontend/index.html plus the real
// call sites in deck.js / onboarding.js / core.js; the only frozen constant is
// the cfg-* id list, which is the point of the test.
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const ROOT = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");

function report(checks) {
  let fail = 0;
  for (const [name, ok] of checks) {
    console.log((ok ? "PASS" : "FAIL") + "  " + name);
    if (!ok) fail++;
  }
  return fail;
}

// The 63 config inputs saveSettings() reads. Adding a setting? Add it here too.
const CFG_IDS = [
  "cfg-adzuna-app-id", "cfg-adzuna-app-key", "cfg-ai-api-key", "cfg-ai-model-fast",
  "cfg-ai-model-strong", "cfg-ai-model-utility",
  "cfg-ai-ondevice-fast", "cfg-ai-ondevice-strong", "cfg-ai-ondevice-utility",
  "cfg-ai-url", "cfg-ashby",
  "cfg-ats-login-password", "cfg-available-start", "cfg-bls-api-key",
  "cfg-certifications", "cfg-city", "cfg-context-window", "cfg-country",
  "cfg-desired-salary", "cfg-disability", "cfg-email", "cfg-exclude",
  "cfg-flaresolverr-url", "cfg-gender", "cfg-github", "cfg-greenhouse",
  "cfg-keywords", "cfg-lever", "cfg-linkedin", "cfg-live-refresh", "cfg-locations",
  "cfg-location", "cfg-middle-name", "cfg-name", "cfg-notice-period",
  "cfg-over-18", "cfg-phone", "cfg-portfolio", "cfg-race", "cfg-recruitee",
  "cfg-salary", "cfg-salary-auto-ingest", "cfg-scoring-tier", "cfg-server-host",
  "cfg-skills", "cfg-sponsorship", "cfg-state", "cfg-street-address",
  "cfg-street-address-2", "cfg-summary", "cfg-sync-enabled", "cfg-sync-folder",
  "cfg-sync-fulfill", "cfg-sync-interval", "cfg-sync-label", "cfg-usajobs-email",
  "cfg-usajobs-key", "cfg-veteran", "cfg-work-auth", "cfg-workable",
  "cfg-workday-email", "cfg-workday-password", "cfg-zip",
].sort();

const EXPECTED_PANES = ["stab-profile", "stab-search", "stab-integrations", "stab-assist", "stab-sync"];

// Basic mode is an allowlist of section-cards; this is it.
const BASIC_CARDS = [
  "Personal Info", "Experience", "Education",           // Profile
  "Job Search",                                         // Job Search
  "AI Connection", "Application Honesty Level", "Resume Visual Style", // AI
  "Apply Assist", "Install the extension",              // Apply
  "Folder Sync",                                        // App
].sort();

const virtualConsole = new VirtualConsole();
const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost:8888/", virtualConsole });
const { window } = dom;
const doc = window.document;
const settings = doc.getElementById("settings");

const checks = [];

// ---- 1. config round-trip: the cfg-* surface is unchanged and unique ----
const cfgFound = Array.from(doc.querySelectorAll('[id^="cfg-"]')).map(e => e.id).sort();
checks.push(["cfg-* id set is unchanged (" + CFG_IDS.length + " inputs)", JSON.stringify(cfgFound) === JSON.stringify(CFG_IDS)]);
if (JSON.stringify(cfgFound) !== JSON.stringify(CFG_IDS)) {
  console.log("   missing:", CFG_IDS.filter(i => !cfgFound.includes(i)));
  console.log("   extra:  ", cfgFound.filter(i => !CFG_IDS.includes(i)));
}
const allIds = Array.from(doc.querySelectorAll("[id]")).map(e => e.id);
const dupes = allIds.filter((id, i) => allIds.indexOf(id) !== i);
checks.push(["no duplicate element ids in index.html", dupes.length === 0]);
if (dupes.length) console.log("   dupes:", dupes);
// every cfg-* input lives inside a settings pane, so saveSettings() sees it
checks.push(["every cfg-* input sits inside a settings pane",
  cfgFound.every(id => doc.getElementById(id).closest(".settings-tab-panel") !== null)]);

// ---- 2. five tabs, five panes ----
const tabs = Array.from(settings.querySelectorAll(".settings-tab"));
const paneIds = tabs.map(b => (/switchSettingsTab\(this,\s*'([^']+)'/.exec(b.getAttribute("onclick")) || [])[1]);
checks.push(["settings has exactly 5 tabs", tabs.length === 5]);
checks.push(["tab buttons target the expected panes", JSON.stringify(paneIds) === JSON.stringify(EXPECTED_PANES)]);
checks.push(["every tab's pane exists", paneIds.every(id => id && doc.getElementById(id))]);
checks.push(["no pane is orphaned (no tab points at it)",
  Array.from(settings.querySelectorAll(".settings-tab-panel")).every(p => paneIds.includes(p.id))]);
checks.push(["exactly one pane starts active",
  settings.querySelectorAll(".settings-tab-panel.active").length === 1]);

// ---- 3. all 34 section-cards survived the move ----
const cards = Array.from(settings.querySelectorAll(".settings-tab-panel .section-card"));
checks.push(["all 34 settings cards are present", cards.length === 34]);

// ---- 4. Basic mode allowlist ----
const title = c => (c.querySelector("h2") || {}).textContent || "?";
const basic = cards.filter(c => c.classList.contains("settings-basic")).map(title).sort();
checks.push(["Basic-mode allowlist is the expected " + BASIC_CARDS.length + " cards", JSON.stringify(basic) === JSON.stringify(BASIC_CARDS)]);
if (JSON.stringify(basic) !== JSON.stringify(BASIC_CARDS)) console.log("   got:", basic);
checks.push(["every tab has at least one Basic card (no empty tab in Basic mode)",
  paneIds.every(id => doc.getElementById(id).querySelector(".section-card.settings-basic"))]);
checks.push(["no card is both Basic and Advanced",
  !cards.some(c => c.classList.contains("settings-basic") && c.classList.contains("settings-advanced"))]);

// ---- 5. deep links from the real call sites ----
const src = f => fs.readFileSync(path.join(ROOT, "js", f), "utf8");
const deck = src("deck.js"), onboarding = src("onboarding.js"), core = src("core.js"), settingsJs = src("settings.js");

const paletteTargets = Array.from(deck.matchAll(/paletteGoSettings\('([^']+)'(?:,\s*'([^']+)')?\)/g));
checks.push(["palette has settings entries", paletteTargets.length >= 5]);
checks.push(["every palette pane target exists", paletteTargets.every(m => !!doc.getElementById(m[1]))]);
checks.push(["every palette card target exists", paletteTargets.every(m => !m[2] || !!doc.getElementById(m[2]))]);

const tourTabs = Array.from(onboarding.matchAll(/_tourSwitchSettingsTab\('([^']+)'\)/g)).map(m => m[1]);
checks.push(["every tour tab switch targets a real tab button",
  tourTabs.every(id => tabs.some(b => (b.getAttribute("onclick") || "").includes("'" + id + "'")))]);

const tourSelectors = Array.from(onboarding.matchAll(/^\s*selector: '((?:[^'\\]|\\.)*)',$/gm))
  .map(m => m[1].replace(/\\'/g, "'"));
checks.push(["tour has selectors", tourSelectors.length >= 10]);
const badSel = tourSelectors.filter(s => !doc.querySelector(s));
checks.push(["every tour step selector resolves in index.html", badSel.length === 0]);
if (badSel.length) console.log("   unresolved:", badSel);

const coreTargets = Array.from(core.matchAll(/includes\("'(stab-[^']+)'"\)/g)).map(m => m[1]);
checks.push(["goAISettings/goAssistSettings target real panes",
  coreTargets.length >= 2 && coreTargets.every(id => !!doc.getElementById(id))]);

const cardRefs = Array.from(settingsJs.matchAll(/getElementById\('(card-[^']+)'\)/g)).map(m => m[1]);
checks.push(["settings.js card references resolve", cardRefs.every(id => !!doc.getElementById(id))]);

// no stale references to the four retired panes anywhere in the frontend
const retired = ["stab-honesty", "stab-answerbank", "stab-prompts", "stab-logs"];
const allJs = fs.readdirSync(path.join(ROOT, "js")).filter(f => f.endsWith(".js")).map(src).join("\n") + html;
checks.push(["no references to retired pane ids remain", retired.every(id => !allJs.includes(id))]);

// ---- 6. the mode mechanism itself ----
// Eval the Basic/Advanced block against the real DOM and check the tab guard.
const modeBlock = settingsJs.slice(settingsJs.indexOf("// ---- Basic / Advanced settings mode ----"));
window.eval(modeBlock);
window.localStorage.setItem("jobsmith_settings_mode", "basic");
window.applySettingsMode();
checks.push(["basic mode sets no advanced class", !settings.classList.contains("settings-mode-advanced")]);
checks.push(["basic mode hides no tab (all 5 have Basic content)",
  settings.querySelectorAll(".settings-tab.settings-tab-hidden").length === 0]);
window.setSettingsMode("advanced");
checks.push(["advanced mode sets the section class", settings.classList.contains("settings-mode-advanced")]);
checks.push(["advanced mode hides no tab", settings.querySelectorAll(".settings-tab.settings-tab-hidden").length === 0]);
checks.push(["mode persists under the original localStorage key",
  window.localStorage.getItem("jobsmith_settings_mode") === "advanced"]);

// A tab whose pane has zero Basic cards must hide in Basic mode.
doc.getElementById("stab-sync").querySelectorAll(".section-card.settings-basic")
  .forEach(c => c.classList.remove("settings-basic"));
window.setSettingsMode("basic");
const syncBtn = tabs.find(b => (b.getAttribute("onclick") || "").includes("'stab-sync'"));
checks.push(["a pane with no Basic cards hides its tab in Basic mode", syncBtn.classList.contains("settings-tab-hidden")]);

const fails = report(checks);
if (fails) { console.error(`\ntest_settings_structure.js: ${fails} check(s) failed`); process.exit(1); }
console.log("\ntest_settings_structure.js: all checks passed");
