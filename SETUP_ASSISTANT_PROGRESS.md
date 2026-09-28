# Setup Assistant — progress log

Spec: `SETUP_ASSISTANT_PRD.md` (exported from the Claude Doc, rev 9; source of truth, do not edit).
Worktree `~/jobsmith-setup`, branch `feat/setup-assistant` (from origin/main 18adfc2). Never touch `~/jobsmith`.

## How to resume
1. Read the PRD, then this file top to bottom. The first milestone not marked DONE is next.
2. `node_modules` is a gitignored symlink to `~/jobsmith-nli/node_modules` (no network install).
3. Tests: `~/jobsmith-nli/.venv/bin/pytest -q -p no:cacheprovider` · `npm run test:frontend` · iOS see PRD test plan.

## Baselines (18adfc2, before any change)
- pytest: 1073 passed, 3 skipped. NOTE: `tests/test_api_auth.py` has 5 order-dependent flaky
  failures (TestDashboardAuthGate x3, TestCsrfGate x1, TestCookieExchange x1) seen in 1 of 3 full
  runs; the file passes 28/28 alone. Pre-existing, not caused by this work.
- frontend: 423 PASS lines, all files "all checks passed".
- iOS: (recorded at M4)

## Milestones
| # | Status | Commit |
|---|--------|--------|
| M1 shared plumbing (desktop backend) | DONE | (see git log "M1") |
| M2 desktop wizard step 0 | DONE | (see git log "M2") |
| M3 desktop fixes | DONE | (see git log "M3") |
| M4 iOS step 0 | todo | |
| M5 iOS fixes | todo | |
| M6 hardening | todo | |

## Provider URL check (M1, 2026-09-28, `curl -s -o /dev/null -w '%{http_code}' <base>/models`)
| Provider | Base URL | HTTP | Verdict |
|---|---|---|---|
| OpenAI | https://api.openai.com/v1 | 401 | OK |
| Anthropic | https://api.anthropic.com/v1 | 401 | OK (bogus Bearer → `authentication_error: Invalid bearer token`, so Bearer auth is accepted on /models) |
| Google Gemini | https://generativelanguage.googleapis.com/v1beta/openai | 000 | Local DNS on this Mac resolves the host to 0.0.0.0 (blocklist). Re-checked once via public DNS: no key → 404, bogus Bearer → 400 "Please pass a valid API key", wrong path → 404. URL is right. Owner: this Mac cannot reach Gemini until the DNS block is lifted. |
| xAI (Grok) | https://api.x.ai/v1 | 401 | OK |
| Mistral | https://api.mistral.ai/v1 | 401 | OK |
| Groq | https://api.groq.com/openai/v1 | 401 | OK |
| DeepSeek | https://api.deepseek.com/v1 | 401 | OK |
| Together AI | https://api.together.xyz/v1 | 401 | OK |
| Fireworks | https://api.fireworks.ai/inference/v1 | 401 | OK |
| Cerebras | https://api.cerebras.ai/v1 | 403 | OK (403 "Not authenticated" with no header; bogus Bearer → 401 `wrong_api_key`) |
| NVIDIA NIM | https://integrate.api.nvidia.com/v1 | 200 | OK |
| OpenRouter | https://openrouter.ai/api/v1 | 200 | OK |
No row needed fixing.

## Decisions
- D1 Presets live in `backend/ai_providers.json` (12 rows: name, base_url, key_url; Custom is NOT a row —
  UIs append it). Served by `GET /api/ai/providers`; bundled via packaging/jobsmith-backend.spec.
  `ai.provider` stores the preset name, or `custom`.
- D2 `_model()` raises `AINotConfigured("No AI model is set up — open Settings → AI")`; a new
  `_configured_model()` (returns "") is used by endpoint resolution so listing /models on a fresh install
  still works. Other raw `local-model` fallbacks (auto_apply/llm_client.py, browser_use_agent.py) are
  left alone: the PRD scopes the fix to `_model()`.
- D3 Error codes (shared with iOS later): auth, credit, model, rate_limit, unreachable, unavailable,
  no_model, error. Order: 401/403 → auth; 402 or insufficient_quota → credit; 404 / model_not_found /
  "model … not found|does not exist" → model; 429 → rate_limit; connection/timeout → unreachable.
- D4 ping uses exactly `max_tokens: 1` (no max_completion_tokens retry): a model that rejects
  max_tokens would fail every real call in the app too, so failing the ping is the honest answer.
- D5 `POST /api/onboarding/ai` fields are optional (None = leave as is) so Local does not wipe the
  endpoint. "Continue anyway" = `verified:false` → `ai_verified: false` (new LOCAL top-level key).
  It also starts the NLI / Quick match downloads when `nli` / `triage` are true.
- D6 config.example.yaml: model ids, Adzuna AND USAJobs placeholders blanked, `api_key: ''` (was
  `lm-studio`; blank already falls back to that placeholder). base_url left at localhost:1234.
- D7 `ai.provider` Swift registry row added in M1 (not M4) because tests/test_sync_crosslang.py
  asserts the two registries match; stale "not keychain" note fixed at the same time.
- D8 Added `POST /api/ai/models {base_url, api_key}` (not in the PRD's file list): the Cloud picker and
  Advanced "Load models" must list with the typed, unsaved values (bug 2). Wraps test_connection; writes nothing.
  `ping_chat` also refuses an empty base_url (the SDK would otherwise default to api.openai.com).
- D9 Desktop tier mapping for Cloud: Writing = strong, Scoring = fast, Quick helpers = utility. "Same as
  Writing" fills all three; a separate Scoring model sets `scoring_tier: fast`; the Quick match box sets
  `local-match-model`. Local: all three = sentinel; box checked → `local-match-model` + nli + triage;
  unchecked → `strong` (Apple) and nli untouched.
- D10 Re-run (profile already exists): step-0 Continue only TESTS; the AI choice rides in the Review diff
  (new rows: AI provider, Local match; URL/key/provider rows treat "absent" as no change). After applying,
  `POST /api/onboarding/ai {mode, verified, triage?, nli?}` records setup_mode and starts downloads only
  for applied rows. "Change setup…" (obChangeSetup, `only:'ai'`) saves directly and closes.
- D11 Switching Cloud provider from the dropdown clears the key (keys are per provider); "Edit address"
  keeps URL and key. Advanced lists every id (no non-chat filter: "unchanged in power") and always keeps a
  saved tier value as an option so re-runs never lose tiers.
- D12 Download chip: `#ob-dl-chip` in the top bar, started after a save that asked for downloads, polls
  triage + nli status every 3 s, hides when nothing is downloading; click → Settings → AI.
- D13 `GET /api/config` ai now also returns `provider` and `nli_beta.enabled` (wizard prefill + diff).
- D14 Résumé chunking budget = min(7000, 8000 − prompt-template length − 200): the 8,000 cap covers the
  whole prompt, so a bare 7,000-char chunk plus the template could still overflow. Sections split on
  headings (known names or short ALL-CAPS lines); an oversized section splits on blank lines, then lines.
  The 16,000-char truncation now applies only to non-Apple models. Merge: first non-empty scalar; skills/
  certs deduped case-insensitively; experience deduped by title+company (bullets merged); education by
  degree+school.
- D15 Error copy: `ai_engine.server_label(cfg)` / JS `aiServerName()` = provider name, else "your AI
  server". LM Studio wording kept only on the context-window "Apply to LM Studio" + reload controls
  (system.py load/reload endpoints included).
- D16 Naming: "Local AI model (beta)" → "Local match" (+ "(on-device, beta)" hint in Settings);
  scoring option → "Quick match (on-device, free)"; NLI reasoning → "Scored by Local match" on BOTH
  desktop (nli/fit.py) and iOS (NLIFit.reasoningPrefix) for parity.
- D17 Job scoring select moved to Basic with Quick match first, AI tiers as "AI model: …". Dropdown change
  only shows status; saveSettings starts the Quick match download. DELETE /api/ai/triage/model returns
  `scoring_tier_reset` and the UI resets the select + toasts.
- D18 Key fields: wizard Adzuna/BLS and Settings USAJobs key are password inputs; `realKey()` blanks the
  old example placeholders (your-app-id / your-app-key / your-api-key / your-email@example.com).

## Verification log
- M1: pytest 1101 passed, 3 skipped (baseline 1073; +28 in tests/test_setup_assistant.py). The only
  existing test changed: tests/test_honesty_prompts.py MINIMAL_CONFIG gained `model: test-model`
  (it relied on the old silent `local-model`). frontend unchanged (423 PASS).
- M2: pytest 1103 passed, 3 skipped. frontend 486 PASS lines, 14/14 files pass. New
  frontend/tests/test_setup_modes.js (in test:frontend); test_apple_intelligence.js wizard half rewritten to
  boot from the real /api/onboarding/status shape (its old "offer only when no endpoint" checks are gone
  with the offer itself).
- M3: pytest 1109 passed, 3 skipped (+6: chunking via a fake 8,000-char engine on a 20k résumé, chunk
  limits, merge rule, real parse error, Quick-match delete reset, server label). tests/test_nli_beta.py
  reasoning prefix updated. frontend 492 PASS: test_triage_ui (no install on pick, Save installs, delete
  resets, Basic placement, labels), test_nli_beta (Retry/Delete with switch off + failed download).
