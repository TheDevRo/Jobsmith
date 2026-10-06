# PRD: Ponytail Cleanup (over-engineering reduction)

**Goal:** cut ~2000 lines and 3 dependencies with no behavior change.
**Source:** ponytail-audit of `main` (2026-10-06). Surface scan; every item must be verified before cutting.
**Out of scope:** new features, bug fixes, perf, the NLI beta branch (`feat/nli-beta`, merge later).

## Rules for the executor
- One PR per phase, each independently mergeable. Tests must pass before and after (`pytest`, `npm test`).
- No behavior change. If a cut changes behavior, stop and note it in the PR instead.
- Do not touch `ios-standalone/` Swift code or `backend/sync/` (cross-language contract).
- Verify each "unused" claim with grep (including tests, docs, Dockerfile, packaging spec) before deleting.

## Phase 1: Safe deletes (est. 30 min)
| # | Change | Verify |
|---|---|---|
| 1.1 | Delete `docker-compose.skyvern.yml` | No references in backend, frontend, tests, docs, README, ARCHITECTURE |
| 1.2 | Inline `backend/auto_apply/utils/browser_helpers.py` (184 lines, only file in the package) into its caller(s), or leave if 3+ callers | Count callers first |
| 1.3 | Dedupe `_extract_json`: keep one, import in the other (`backend/resume_parser.py:164`, `backend/auto_apply/llm_client.py:431`) | Return types differ (`dict` vs `list | dict`); keep the broader one, resume_parser asserts dict |
| 1.4 | Review root planning docs and `n8n/workflows.json`; delete the stale ones. README mentions n8n, so check that section | Only tracked files; list what was removed in the PR |

**Done when:** tests green, net negative diff.

## Phase 2: Dependency drops (est. 1-2 hrs)
| # | Change | Notes |
|---|---|---|
| 2.1 | Replace `feedparser` with `xml.etree.ElementTree` in `backend/job_sources/weworkremotely.py:95` | Keep the same output fields; add one fixture-based test |
| 2.2 | Switch BeautifulSoup parser `"lxml"` to `"html.parser"` (`job_sources/_generic.py:24,82`, `job_sources/manual.py:121`), drop `lxml` | Check parse results on saved fixtures; if any differ, keep lxml and drop this item |
| 2.3 | Migrate `aiohttp` (16 files) to `httpx`, drop `aiohttp` | Largest item. Tests mock aiohttp (e.g. `test_ssrf_guard.py`, `test_ashby_fetcher.py`, `test_greenhouse_fetcher.py`), so mocks need rewriting. Do in sub-PRs by module. Cancel this item if the SSRF guard depends on aiohttp specifics |

Update `requirements.txt`, regenerate `requirements.lock`, check `Dockerfile` and `packaging/jobsmith-backend.spec` (PyInstaller hidden imports).

**Done when:** `pip install -r requirements.lock` has none of the dropped packages; app boots; tests green.

## Phase 3: Simplify LLM JSON repair (est. 2 hrs)
- `backend/auto_apply/llm_client.py:431-619`: ~180 lines of hand-rolled repair (quote swap, trailing commas, trailing prose).
- Replace with provider JSON mode / structured output where the provider supports it, plus `json.JSONDecoder().raw_decode` for trailing prose.
- Risk: local/weaker models (Ollama etc.) may rely on the repair. Keep a minimal fallback if `tests/auto_apply/test_llm_mapping.py` cases fail. Do not delete tests to make it pass.

## Phase 4: Needs an owner decision (do NOT execute without approval)
- **4.1 Two apply engines.** `browser_use_agent.py` (1230 lines) + `test_browser_use_agent.py` (1309 lines) + `requirements-optional.txt`. Flag `auto_apply.use_browser_use` defaults to `False` (`background_tasks.py:787`), and the setting is LOCAL/not synced. Options: (a) keep, (b) move to a branch/tag and remove from main. Removal also needs edits to `background_tasks.py`, `settings_registry.py`, docs.
- **4.2 Duplicated JS and sync checkers.** `extension/src/common/fill.js` and `ios-standalone/App/Apply/JS/fill.js` are kept in sync by `scripts/check_apply_js_sync.py`. Same pattern in `tools/sync-crosslang` and `tools/ai-crosslang`. Option: single source plus a copy step at build time.

## Success metrics
- Lines removed (target ≥ 2000 including Phase 4.1 if approved, ≥ 400 without it).
- Dependencies removed: `feedparser`, `lxml`, `aiohttp`.
- All existing tests pass; no test deleted except those covering removed code.

## Execution notes for the cloud session
1. Work on a branch from `origin/main` (local `main` is 51 commits behind; fetch first).
2. Do Phase 1, open PR, then Phase 2, and so on. Stop after Phase 3.
3. PR descriptions list: what was removed, how it was verified, lines changed.
