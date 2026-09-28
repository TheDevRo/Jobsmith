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
| M1 shared plumbing (desktop backend) | in progress | |
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
- (log here)

## Verification log
- (per milestone)
