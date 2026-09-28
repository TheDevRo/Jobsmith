# Jobsmith Setup Assistant PRD — Local / Cloud / Advanced

Sep 28, 2026 · @Deven

## Context and goals

First-run setup must reach a working AI in 1–2 taps (Local) or one pasted key (Cloud), on desktop and iOS. Today both platforms open on a raw form (OpenAI-compatible URL, key, three model tiers) that defaults to `localhost:1234`, has no provider presets, and hides the on-device models in Settings.

**Decisions (owner, 2026-09-28)**

- Platforms: desktop and iOS, same three-card flow: **Local**, **Cloud**, **Advanced**.
- Local = Apple Intelligence on **all** tiers, including résumé and cover-letter writing, with a plain note that writing quality is basic.
- Local recommends (pre-checked) downloading **Quick match** and **Local match** for faster scoring and Apply Assist.
- Cloud = provider presets plus **Custom**. Custom takes any OpenAI-compatible API (LM Studio, Ollama, vLLM on a personal rig); its API key is optional. Nothing is labelled "LM Studio".
- Models: the user picks from the provider's live model list. We ship **no** default model IDs; free tiers on OpenRouter and NVIDIA NIM change too often to maintain.
- Advanced = today's full AI form, unchanged in power.

**Non-goals**

- No accounts, no Jobsmith-hosted AI, no bundled API keys.
- No per-provider default model lists, no pricing data.
- No change to later wizard steps beyond the bug fixes listed here.
- No release, push or TestFlight upload by the builder.

## Build setup and rules

Work only in the worktree `~/jobsmith-setup`, branch `feat/setup-assistant`, cut from `origin/main` at 18adfc2 (iOS build 53).

- **Never touch `~/jobsmith`.** Its local `main` is 40+ commits stale and holds the owner's uncommitted `.gitignore`.
- Keep a progress log at `~/jobsmith-setup/SETUP_ASSISTANT_PROGRESS.md`. Commit it with every milestone so work survives a usage cut-off; a fresh agent must be able to resume from it alone.
- Commit per milestone with the repo's attribution trailer. Do not push, tag, release or upload to TestFlight.
- Python tests: use the venv at `~/jobsmith-nli/.venv`. Tests must never hit the network (the NLI and triage download URLs are already blanked by an autouse fixture; do the same for any new network call).
- Reuse before writing: Apple sentinel routing (`backend/apple_bridge.py`, iOS `EngineRouter`), the NLI and triage install/status APIs, iOS `AIConfig.SavedEndpoint`, the desktop re-run diff step, and the settings-sync registry (`backend/sync/settings_registry.py`, iOS `SettingsSync.swift`).
- Python and Swift behaviour must stay in parity wherever both platforms implement the same rule.
- If a decision is not covered here, pick the simplest option that satisfies the acceptance criteria and record it under "Decisions" in the progress log.

## Target flow

Step 0 replaces today's "Connect AI" form with three cards; every path saves a tested AI config before the Résumé step runs.

&#91;embedded content: setup step 0 · three paths, one shared exit\]

**Step 0 header:** "How should Jobsmith think?" Sub-line: "You can change this any time in Settings → AI."

### Local card

- Title "Local". Body: "Runs privately on this device with Apple Intelligence. Free, nothing to set up. Writing quality is basic; you can connect a cloud provider later for stronger résumés."
- Enabled only when Apple Intelligence reports available. Otherwise greyed out, with the real reason: unsupported hardware/OS ("Needs a Mac with Apple silicon on macOS 26+" / "Needs an iPhone with Apple Intelligence on iOS 26+"), turned off ("Turn on Apple Intelligence in System Settings, then tap Check again"), or model still downloading. Include a **Check again** button.
- On select: all three tiers = `apple-on-device`.
- Checkbox, **pre-checked**, labelled "Recommended": "Download Quick match and Local match (about N MB) for faster job scoring and smarter Apply Assist." N is computed from the pinned file sizes per platform (today: desktop about 134 + 690 MB, iOS about 62 + 399 MB); never hard-code the number in copy.
- When checked: scoring uses Quick match (`scoring_tier = local-match-model` on desktop, `fastModel = local-match-model` on iOS) and the Local match switch is on (`nli_beta.enabled` / `nliBetaEnabled`). Downloads start on Continue, in the background.
- A download progress row stays visible after the wizard closes (desktop: Settings → AI plus a small status chip in the app header or checklist; iOS: Settings → AI). Until Quick match is ready, scoring falls back to Apple Intelligence, never to an unconfigured endpoint.
- iOS: on cellular, ask once before downloading ("Download N MB on cellular?" Wait for Wi-Fi / Download now).

### Cloud card

- Title "Cloud". Body: "Use an AI provider such as OpenAI, Anthropic or OpenRouter. You bring the server address and an API key. Your provider may charge per use."
- Provider picker from the preset table below; **Custom** is last.
- Preset selected: base URL filled and read-only (with an "Edit address" link that switches to Custom with the URL kept), API key field required, and a "Get an API key" link opening the provider's key page.
- Custom selected: base URL field (placeholder `https://your-server/v1`), API key marked "optional". Help text: "Any OpenAI-compatible server, including one you run yourself." Normalise the URL: add `https://` if no scheme, strip trailing `/chat/completions`, and warn (do not block) if it lacks `/v1`.
- After a key is entered (or immediately for Custom): fetch the model list into a **searchable** picker. If listing fails or returns nothing, show a free-text "Model ID" field with the provider's error in plain English.
- The user picks one **Writing model**. "Scoring" and "Quick helpers" default to "Same as Writing model", behind a collapsed "Use different models for scoring" disclosure.
- Optional, unchecked: "Also download Quick match (about N MB) for faster, free job scoring."
- Do not auto-select `models[0]`. Skip obvious non-chat IDs (containing `embed`, `whisper`, `tts`, `rerank`, `moderation`, `dall-e`, `image`) from the default list, with a "show all" toggle.

### Advanced card

- Title "Advanced". Body: "Full control: any server, a different model per task, and local model switches."
- Opens today's full AI form (desktop step-0 fields plus the Apple and local-model controls from Settings → AI; iOS `AIConnectionSettingsView`), with the preset picker above the URL field as a shortcut.

### Shared exit: test, save, continue

- **Continue** runs a real test: a 1-token chat completion (`max_tokens: 1`, one user message "ping") to the chosen Writing model; for Local, the Apple availability check plus the same ping through the bridge/engine.
- Map failures to plain English: 401/403 "That API key was rejected"; 404 or model-not-found "That model is not available on this account"; 402/insufficient quota "Your provider account has no credit"; 429 "The provider is rate-limiting; try again in a minute"; connection/DNS/TLS "Could not reach the server at \<host>". Always offer "Show details" with the raw error.
- On success, **save the AI section immediately** (base URL, key, all tier models, scoring tier, local-model switches, `setup_mode`), so the Résumé step and "Suggest titles" use it.
- On failure: stay on step 0; also offer "Continue anyway" (saves, marks AI unverified) and "Set up AI later" (saves nothing AI-related).
- Store `setup_mode` = `local` | `cloud` | `advanced` per device, never synced. Settings → AI gets **Change setup…**, which reopens only step 0 and returns to Settings when done.

## Cloud provider presets

Each preset is three strings: name, base URL, API-key page. There are 12 presets plus Custom, in this order. The table is from memory. **The builder must verify every base URL** with `curl -s -o /dev/null -w '%{http_code}' <base>/models`, where 200 or 401 means the URL is right. Record the results in the progress log, and fix any row that fails.

| Provider | Base URL | API key page | Notes |
| --- | --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | platform.openai.com/api-keys |  |
| Anthropic | `https://api.anthropic.com/v1` | console.anthropic.com/settings/keys | OpenAI-compatibility layer. Confirm that `/models` listing works with Bearer auth; if it doesn't, use the free-text model ID fallback. |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | aistudio.google.com/apikey | Path is not `/v1`, so the Custom-URL `/v1` warning must not fire for presets. |
| xAI (Grok) | `https://api.x.ai/v1` | console.x.ai |  |
| Mistral | `https://api.mistral.ai/v1` | console.mistral.ai/api-keys |  |
| Groq | `https://api.groq.com/openai/v1` | console.groq.com/keys | Free tier |
| DeepSeek | `https://api.deepseek.com/v1` | platform.deepseek.com/api\_keys |  |
| Together AI | `https://api.together.xyz/v1` | api.together.ai/settings/api-keys |  |
| Fireworks | `https://api.fireworks.ai/inference/v1` | fireworks.ai/account/api-keys |  |
| Cerebras | `https://api.cerebras.ai/v1` | cloud.cerebras.ai | Free tier |
| NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | build.nvidia.com | Free tier; large model list, so search matters |
| OpenRouter | `https://openrouter.ai/api/v1` | openrouter.ai/keys | `/models` is public. A bad key only shows up in the chat ping, which is why the ping is required. |
| Custom | user-entered | none | Key optional. Covers LM Studio, Ollama, vLLM, llama.cpp, LiteLLM and a personal rig. |

**Where the table lives:** desktop keeps one JSON/py constant served by `GET /api/ai/providers`, and the frontend reads it from there. iOS keeps a Swift constant with the same rows. A unit test on each side asserts the two lists match: same names and URLs, same order.

**What gets stored:** the `ai.base_url` value. Also store `ai.provider` as the preset name, or `custom`, so re-opening the wizard shows the right choice. `ai.provider` syncs alongside `ai.base_url`; add it to both registries.

## Desktop implementation

The wizard stays one overlay in `frontend/index.html` driven by `frontend/js/onboarding.js`. Step 0 becomes the three cards, and the backend gains a providers list and a real test endpoint.

**Files to change**

- `frontend/index.html`: replace the step-0 panel (the "Connect AI" block, roughly lines 1236–1257) with the cards and the Cloud and Advanced sub-panels. Rename the stepper label to "AI". Add "Change setup…" to Settings → AI.
- `frontend/js/onboarding.js`
  - Rewrite `obTestAI`, `obApplyAIStatus`, `obUseAppleIntelligence`, `obPopulateModels` and `obBuildPayload` around a single `_obState.ai = {mode, provider, base_url, api_key, models, scoring_tier, nli, triage}`.
  - Keep `obFinish`, the tour and the re-run diff step.
- `backend/routers/settings.py`
  - Add `GET /api/ai/providers` and `POST /api/ai/test-chat {base_url, api_key, model}`. Neither one writes config.
  - Add `on_device` to `GET /api/onboarding/status`.
  - Add `POST /api/onboarding/ai`, which saves only the AI section plus `setup_mode` and is used by the shared exit.
- `backend/ai_engine.py`
  - Add `ping_chat()`: a 1-token completion with a 20 s timeout that returns `{ok, code, message, detail}`. Route the Apple sentinel through the bridge.
  - Keep `test_connection` for Settings, but have its errors go through the same plain-English mapper.
- `backend/resume_parser.py`: fit Apple's 8,000-character input cap (`apple-bridge/Sources/JobsmithAppleAI/OnDeviceModel.swift:17`).
  - When the strong tier is `apple-on-device`, split the résumé on section boundaries into chunks under 7,000 characters, parse each one, and merge: first non-empty scalar wins, lists are concatenated and de-duplicated.
  - A parse that still fails returns the real error, never a generic message.
- `backend/sync/settings_registry.py`: add `ai.provider` (synced) and `setup_mode` (LOCAL).
- `config.example.yaml`
  - Blank the example model IDs (a fresh install must not carry `mistral-7b`) and the Adzuna placeholders (`your-app-id`, `your-app-key`).
  - Add `ai.provider: ""`.

**Bugs and QoL fixes (desktop)**

1. `/api/onboarding/status` omits `on_device`, so the Apple option never shows when the wizard opens. Fix: include it, and give the Local card real status on first paint.
2. Test connection saves config immediately and swallows errors (`catch (e) {}`), which also bypasses the re-run review. Fix: test through `test-chat` with values passed in the request. Only the shared exit (or the re-run diff) saves.
3. The wizard can Finish with empty models, writing `""` and then hitting confusing `local-model` errors. Fix: the shared exit requires a tested Writing model or an explicit "Set up AI later". `_model()` should raise a clear "No AI model is set up — open Settings → AI" instead of sending `local-model`.
4. `obPopulateModels` auto-picks `models[0]` for all tiers. Fix: no auto-pick. Filter out non-chat model IDs, and give the picker search.
5. `_obState.onDevice` goes stale after a re-test and saves the wrong `scoring_tier`. Fix: derive the tier from the `_obState.ai.mode` snapshot at save time.
6. Error copy always names LM Studio (`settings.js:1422` "LM Studio not reachable", the `settings.py:588` 504 text, the tour text at `onboarding.js:797`). Fix: say "your AI server", or the provider name when `ai.provider` is set. Keep LM Studio wording only on the LM Studio-specific load/reload controls.
7. The copy names stale Settings tabs ("Integrations", "Search", "Apply Assist"). Fix: use the real tabs — AI, Job Search, Apply — and grep the whole frontend for the old names.
8. The Résumé step says "Nothing is sent to the cloud" in every mode. Fix: per-mode copy. Local says "stays on this Mac"; Cloud says "is sent to \<provider> to read it".
9. The Adzuna key fields are `type="text"` and show placeholder values. Fix: password type for keys, blank when the value is still the placeholder.
10. Quick match downloads start when the dropdown changes, before Save. Fix: start them only on Save or wizard exit.
11. Deleting Quick match while it is still the scoring tier silently re-installs it (`ai_engine.py` \~599). Fix: deleting it also resets `scoring_tier` to `strong`, with a toast.
12. The Local match Retry button is hidden when the switch is off. Fix: show Retry and Delete whenever there is a failed or partial download.
13. Quick match is Advanced-only in Settings. Fix: move the scoring choice to Basic as "Job scoring: Quick match (on-device, free) / AI model".
14. Naming is inconsistent. Fix: use **Quick match** (bge triage) and **Local match** (NLI) everywhere. That covers the Settings labels, the score labels (`jobs.js` `scoreSourceLine`), `nli/fit.py` reasoning text, and the docs. Retire "Local AI model (beta)" and "Local match model (Quick match…)".
15. Skip for now permanently hides the wizard with no hint where it went. Fix: keep the behaviour, but toast "You can run setup again from Settings → App".
16. Update `docs/getting-started-desktop.md` to match the new flow.

## iOS implementation

The iOS AI step stops embedding the full Settings screen. It gets its own compact three-card view. `AIConnectionSettingsView` stays as the Advanced path and as the Settings screen.

**Files to change** (all under `ios-standalone/`)

- `App/Screens/OnboardingFlow.swift`: the `.ai` step shows a new `SetupModeStep` view instead of `AIConnectionSettingsView()`. Also make the profile import error surface the real engine error, not "Is your AI connected (previous step)?"
- New `App/Screens/SetupModeStep.swift`: the cards, the Cloud provider picker, the searchable model list (`.searchable` over the fetched IDs), and the shared exit.
- `JobsmithKit/.../AI/OpenAICompatibleEngine.swift` and `AIEngine.swift`
  - Add `pingChat(model:)`: a 1-token completion that returns a typed error which the shared plain-English mapper can read.
  - `testConnection` keeps `/models` for listing only.
- `JobsmithKit/.../Core/AppConfig.swift`
  - Add `setupMode` (device-local), `provider` (synced), and `onboardingComplete` (device-local).
  - Add the provider preset constant, with the same rows as desktop.
- `JobsmithKit/.../AI/ResumeProfileParser.swift`: on-device chunking that mirrors the desktop rule (chunks under 7,000 characters, same merge rule).
- `JobsmithKit/.../Sync/SettingsSync.swift`: add the `ai.provider` row. Fix the stale comment that says the API key is not in the Keychain.
- `App/JobsmithStandaloneApp.swift`: gate onboarding on `!config.onboardingComplete` after `ConfigStore` finishes loading. Await the load itself instead of the 300 ms sleep.
- `QuickMatch.swift` and `NLIModelStore.swift`: move downloads to a background `URLSessionConfiguration.background` session. Downloads must survive leaving the screen and the app being backgrounded. Keep the SHA-256 checks.
- `App/Screens/AIConnectionSettingsView.swift`: add the Advanced-path tidy-ups below, plus a "Change setup…" row.

**Bugs and QoL fixes (iOS)**

1. There is no completion flag, so a user who skips profile import gets the wizard on every launch. Fix: Finish and Skip set `onboardingComplete`. Existing users with a non-empty profile are migrated to `true`.
2. The 300 ms sleep races the config load, so the wizard can appear for users who already finished setup. Fix: await the load.
3. The wizard's first view auto-tests the `localhost:1234` default and shows a red error. Fix: nothing auto-tests until the user has entered values. Drop the `localhost` default for new installs; Custom starts empty.
4. One toggle silently starts about 460 MB of downloads. There is no cellular check and Quick match has no Stop button. Fix: ask before downloading on cellular (`NWPathMonitor` `isExpensive`), add Stop for both models, and show a real `ProgressView`.
5. Delete buttons show while the feature is off. Fix: show Delete only when a model is on disk, with a confirmation.
6. Quick match downloads but is never used unless the picker is also set. Fix: turning on Quick match (in the wizard or in Settings) sets `fastModel = local-match-model`. Turning it off restores the previous value.
7. Choosing Local match with no chat AI leaves profile import broken. Fix: the Local card always pairs local models with Apple Intelligence, and the Advanced path warns when the Writing tier is empty.
8. The same tier has three names: "Resume model", "Writing model" and "Resume & cover letters". Fix: use one set everywhere — **Writing**, **Scoring**, **Quick helpers** — with the same subtitle copy as desktop.
9. `NSLocalNetworkUsageDescription` names only LM Studio. Fix: "Jobsmith connects to AI servers on your network, such as one you run on your own computer."
10. Settings are saved only in `onDisappear`, so killing the app mid-step loses them. Fix: the wizard saves on the shared exit. Settings saves on change, debounced.

## Milestones and acceptance criteria

There are six milestones, desktop first. Each one ends with green tests, a commit, and a progress-log entry.

1. **M1 — Shared plumbing (desktop backend).**
   - The providers endpoint is in place, and every URL has been verified with curl.
   - `test-chat` and `ping_chat` work, with the plain-English error mapper.
   - `/api/onboarding/status` returns `on_device`.
   - `POST /api/onboarding/ai` works.
   - `ai.provider` and `setup_mode` are in the registry.
   - `config.example.yaml` has no example model IDs and no Adzuna placeholders.
   - `_model()` raises a clear error instead of sending `local-model`.
   - pytest is green.
2. **M2 — Desktop wizard step 0.**
   - The three cards work, including the Local disabled state with a reason and Check again.
   - The Cloud preset and Custom paths work: searchable picker, free-text fallback, no auto-pick.
   - The Advanced path works.
   - The shared exit saves before the Résumé step.
   - Change setup… appears in Settings → AI.
   - The jsdom tests are updated and new tests added. `test_apple_intelligence.js` now boots from the real `/api/onboarding/status` shape.
3. **M3 — Desktop fixes.**
   - Desktop bugs 6–16 are fixed.
   - Résumé chunking for Apple is in place, with a unit test that feeds a 20,000-character résumé through a fake 8,000-character engine.
   - Quick match and Local match naming is consistent across the UI, labels and docs.
4. **M4 — iOS step 0.**
   - `SetupModeStep` works, with provider presets whose parity with desktop is enforced by a test.
   - `pingChat` works.
   - The `onboardingComplete` gate is awaited on config load.
   - The Local card works, including the unavailable reason on the Simulator.
   - The shared exit saves.
5. **M5 — iOS fixes.**
   - iOS bugs 3–10 are fixed, including background downloads, the cellular prompt, and Stop for both models.
   - Résumé chunking matches desktop.
   - Tier names are unified.
6. **M6 — Hardening.**
   - Run a self-review pass over the diff for correctness.
   - Run the full test suites.
   - Write a final report in the progress log: what changed, what was verified and how, and any open questions.

**Definition of done**

- A new user on this Mac can pick Local, tick nothing else, and import a résumé successfully.
- A new user can pick Cloud → OpenRouter, paste a key, search for a free model, pass the test, and import a résumé.
- A new user can pick Cloud → Custom with no key against an LM Studio server and complete setup.
- A bad key, a wrong model, or an unreachable host each produce the specific plain-English message. None of them leaves a half-saved config.
- Re-running the wizard never writes config before the review step.
- No UI string anywhere says "LM Studio" except the LM Studio-specific controls.
- Existing users are not re-prompted on either platform after upgrading.

## Test plan and verification

Automated suites must stay green at every milestone. Manual checks are done by the planner after the builder reports.

**Automated (builder)**

- Python: `~/jobsmith-nli/.venv/bin/pytest` from the worktree root. New tests:
  - the providers list
  - the `ping_chat` error mapper, with a mocked OpenAI client for 401, 404, 402, 429 and connection errors
  - `on_device` present in `/api/onboarding/status`
  - `POST /api/onboarding/ai` payloads for each mode
  - `_model()` raising a clear error on empty
  - résumé chunking
  - deleting Quick match resetting the scoring tier
  - registry rows
- Frontend: `npm run test:frontend`. Add `frontend/tests/test_setup_modes.js` to that script. It covers:
  - the three cards
  - the Local disabled reason
  - preset selection filling a read-only URL
  - Custom with an empty key
  - no auto-pick
  - the non-chat filter
  - the free-text fallback
  - that the shared exit posts once and before the Résumé step
  - re-run mode never posting config before the diff
- iOS: after adding files, run `xcodegen generate` in `ios-standalone/`.
  - Kit tests: `xcodebuild -scheme JobsmithKit -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test`. Add tests for preset parity with desktop (read the desktop JSON from the repo), the error mapper, `onboardingComplete` migration, and chunking.
  - UI smoke test: pre-boot the simulator first (see the known "Busy / preflight" gotcha). Add a smoke test that launches **without** `-SkipOnboarding` and checks that the three cards show and that Local shows its unavailable reason on the Simulator.
  - `EndToEndWalkthroughTests.testFullPipelineWalkthrough` fails the same way on a clean baseline, so it is not a regression.
- Network: no test may touch the network. Only the curl URL check in M1 does, and it is run by hand and logged.

**Manual (planner, after the builder reports)**

1. Desktop dev server with a fresh `JOBSMITH_HOME`: Local path on this Mac (Apple Intelligence is on). Import a résumé; the Quick match and Local match downloads show progress and finish.
2. Fresh home: Cloud → OpenRouter with a real key and a free model. Pass the test, then import a résumé. Repeat with a wrong key and with a wrong model ID, and confirm the specific messages.
3. Fresh home: Cloud → Custom, `https://lmstudio.thedevro.rocks/v1`, no key. Setup completes.
4. Existing home (this Mac's real config): the wizard does not reappear. Settings → AI → Change setup… round-trips without losing tiers.
5. iOS Simulator: fresh install shows the cards. Custom works against the LM Studio URL. Kill the app mid-wizard, relaunch, and confirm the wizard resumes rather than being lost or looping.
6. On-device iOS check (owner, via TestFlight later): the Local path, the cellular prompt, and a background download that survives backgrounding the app.

Open question for the owner: none blocking. The builder records any judgement calls under "Decisions" in the progress log.
