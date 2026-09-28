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
| M2 desktop wizard step 0 | todo | |
| M3 desktop fixes | todo | |
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

## Verification log
- M1: pytest 1101 passed, 3 skipped (baseline 1073; +28 in tests/test_setup_assistant.py). The only
  existing test changed: tests/test_honesty_prompts.py MINIMAL_CONFIG gained `model: test-model`
  (it relied on the old silent `local-model`). frontend unchanged (423 PASS).
