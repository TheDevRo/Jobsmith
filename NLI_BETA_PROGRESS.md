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
- Owner decisions: model hosting (or fp32 public alternative); Docker build when a daemon is available.

## Acceptance runs (2026-09-26)
- pytest (not integration): 1040 passed. `npm test` (extension + all jsdom suites incl. test_nli_beta.js): pass.
  ruff: clean. Off mode: subprocess test proves onnxruntime/tokenizers/numpy/backend.nli.* never imported.
- Desktop: `scripts/build_desktop.sh --sidecar-only` + `npx tauri build` built Jobsmith.app + DMG (not distributed).
  Sidecar 122.1 MB vs 99.0 MB for the pre-branch commit (5f7e06b, same venv) = +23.1 MB.
  Packaged sidecar smoke (isolated HOME, port 18888, scratch/smoke_sidecar.sh): switch off -> health ok, status off,
  no onnxruntime dylib mapped, Retry with no URL -> the "not available for download yet" error; switch on via
  PUT -> downloaded from a local file server (JOBSMITH_NLI_MODEL_URL) -> ready; /api/ext/scan filled
  Yes / Denver / 8 years, left relatives + essay blank (LLM endpoint dead), model load 1.2 s CPU; switch off ->
  the same scan went back to the LLM path.
- iOS JobsmithKit: 458 tests passed (no Swift touched).
- Docker: NOT RUN. OrbStack was stopped; `orbctl start` hung in "Starting" for 10+ min, stopped it again.
  `uv pip compile requirements.lock` for linux x86_64 / py3.12 resolves (onnxruntime 1.30.0, tokenizers 0.23.2, numpy 2.5.3).

## Decisions / departures
- `FieldValue.source`: existing values only (extractive fills `profile`, essays `llm_generated` with
  confidence 0.5, unanswered `skip`). No new field: the extension already colours `llm_generated` as a
  warning and the desktop Assist flags confidence < 0.6, so essays read as AI drafts with no consumer change.
- Handlers hard-code the tuned prototype config (joined premise for choices, "answer" template, no
  contradiction mode for Yes/No); unused tuning knobs and the resume "wording" facts were not ported.
- Switch is set through its own `PUT /api/settings/nli-beta` (the per-setting endpoint pattern); turning it on
  starts the download server-side. Status adds `installed` so Delete is offered while the switch is off.
- Model URL override: env `JOBSMITH_NLI_MODEL_URL` (base URL; files fetched as `<base>/model.onnx` and `<base>/tokenizer.json`).
- Download resumes from the `.part` file with an HTTP Range request; the model dir only ever gets verified files.
- Fit scoring with no requirement lines (or a local-model error) stays `ScoringUnavailable` (unscored, retried
  later) rather than inventing a neutral 50.

- Model: no public export passes parity (public int8 exports: 72-73% same decisions; q4 96.75%; fp16 99.35%
  but 872 MB; fp32 100% but 1.74 GB). Shipped a local build: ~/jobsmith-extractive/eval/quantize_onnx.py on the
  public fp32 Xenova export (a70e12f8): 8-bit MatMul blocks (accuracy_level 4) + int8 embedding, 682 MB,
  sha256 565e4c99..., deterministic. Total download 691 MB (PRD estimated ~450 MB). Table:
  ~/jobsmith-extractive/results/onnx_parity.md.
- CPU execution provider only: CoreML takes 1416/3791 nodes in 196 partitions and is 3.3x slower.

## Blockers
- Hosting: the shipped model is a local build, so it needs a download location the owner approves
  (`DEFAULT_BASE_URL` in backend/nli/model.py is empty; status then says so and Retry is offered).
  Alternative with no hosting: point FILES at the public fp32 export (1.74 GB, 100% parity, slower).

## Measured numbers
- Port check: in-app pipeline replaying the prototype's PyTorch probabilities = held-out 101 correct / 5 misfills,
  overall 211 / 8, 0 hallucinations (identical to p2fix_extractive).
- ONNX vs PyTorch (631 pairs, 462 fields): mean |dP| 0.0041, max 0.109, 100% same fill/skip and value.
- Real-model eval (`eval.run_eval --run app`, shipped ONNX, NLI only): held-out 101 / 5, overall 211 / 8,
  0 hallucinations; latency per form median 0.55 s, max 1.16 s. With qwen3.5-9b-mtp essays: same numbers,
  25/42 essays filled + flagged, latency median 1.22 s, max 7.46 s.
- Fit scoring latency (240 = 6 gold profiles x 40 bench postings): median 1.78 s, p90 3.15 s, max 3.82 s;
  model load 1.3 s.
- Baseline full pytest on the branch before changes: 1014 passed (one earlier run had 5 order-dependent
  `test_api_auth.py` failures that did not reproduce: pre-existing flake).
