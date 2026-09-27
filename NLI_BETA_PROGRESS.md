# NLI beta: build progress

PRD: `~/jobsmith-extractive/NLI_BETA_PRD.md`. Branch `feat/nli-beta` (worktree `~/jobsmith-nli`). Never pushed.

## Done
- Port: `backend/auto_apply/extractive/` (facts, retrieve, handlers + `fill()`), wired into
  `LLMClient.map_fields_to_values` pass 4 behind `nli.enabled(cfg)`.
- `backend/nli/`: `__init__` (switch, `get_scorer`, `NLIScorer` protocol, status), `model.py` (download /
  verify / resume / delete), `runtime.py` (onnxruntime + tokenizers, cached session), `fit.py` (bench flat
  scoring). `ai_engine.score_job_fit` falls back to it on `ScoringUnavailable` / `BridgeUnavailable`.
- API: `GET /api/ai/nli/status`, `PUT /api/settings/nli-beta`, `POST /api/ai/nli/install`, `DELETE /api/ai/nli/model`.
- Settings UI (AI tab, AI Connection card, under the Apple controls) + `frontend/tests/test_nli_beta.js`.
- Config `ai.nli_beta.enabled` (example config), registry row `Cls.LOCAL` (never syncs).
- Tests: `tests/test_nli_beta.py` (25) incl. off-mode subprocess import check, gold-set invariant, download flow.

## Next
- Pick the ONNX file (parity), pin SHAs, measure eval/latency, build sidecar, Docker, iOS Kit tests, smoke.

## Decisions / departures
- `FieldValue.source`: existing values only (extractive fills `profile`, essays `llm_generated` with
  confidence 0.5, unanswered `skip`). No new field: the extension already colours `llm_generated` as a
  warning and the desktop Assist flags confidence < 0.6, so essays read as AI drafts with no consumer change.
- Handlers hard-code the tuned prototype config (joined premise for choices, "answer" template, no
  contradiction mode for Yes/No); unused tuning knobs and the resume "wording" facts were not ported.
- Switch is set through its own `PUT /api/settings/nli-beta` (the per-setting endpoint pattern); turning it on
  starts the download server-side. Status adds `installed` so Delete is offered while the switch is off.
- Model URL override: env `JOBSMITH_NLI_MODEL_URL` (base URL; files fetched as `<base>/<repo path>`).
- Download resumes from the `.part` file with an HTTP Range request; the model dir only ever gets verified files.
- Fit scoring with no requirement lines (or a local-model error) stays `ScoringUnavailable` (unscored, retried
  later) rather than inventing a neutral 50.

## Blockers
- (none yet)

## Measured numbers
- Baseline full pytest on the branch before changes: 1014 passed (one earlier run had 5 order-dependent
  `test_api_auth.py` failures that did not reproduce: pre-existing flake).
