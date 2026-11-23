"""Quick match (embedding triage): golden parity with the bench, routing (Local match model as the scoring tier),
the refine pass, and the model download. No model needed: the golden fixture carries the embedding of every text
it uses, so a lookup table stands in for the ONNX model. The same fixture drives the Swift twin (TriageTests)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from backend import ai_engine, nli
from backend.nli import triage as TR
from backend.nli import triage_model as TM

GOLDEN = json.loads((Path(__file__).resolve().parent / "fixtures" / "triage_golden.json")
                    .read_text())
QUICK = {"ai": {"base_url": "http://mock.invalid/v1", "scoring_tier": "local-match-model"}}


def lookup(texts):
    return np.array([GOLDEN["embeddings"][t] for t in texts])


def golden_triage():
    return TR.Triage(GOLDEN["data"], lookup)


@pytest.fixture(autouse=True)
def _no_real_model_downloads(monkeypatch):
    monkeypatch.setattr(TM, "DEFAULT_BASE_URL", "http://127.0.0.1:9")  # never the hosted release
    TR.unload()
    TM._job.update(error=None, done=0)


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda c: f"{c['profile']}-{c['job']}")
def test_golden_matches_the_bench(case):
    """Same requirement lines, per-line P(met) and job score as laya-bench triage_eval on the same vectors."""
    ev = golden_triage().evaluate(GOLDEN["jobs"][case["job"]], GOLDEN["profiles"][case["profile"]],
                                  date.fromisoformat(GOLDEN["today"]))
    if case["score_raw"] is None:
        assert ev is None
        return
    lines, p, raw, preview = ev
    assert lines == case["lines"] and preview == case.get("preview", False)
    assert np.allclose(p, case["p"], atol=1e-4)
    assert raw == pytest.approx(case["score_raw"], abs=0.01)


def test_score_report_bucket_and_clamp():
    t = golden_triage()
    today = date.fromisoformat(GOLDEN["today"])
    b = GOLDEN["data"]["buckets"]
    for case in GOLDEN["cases"]:
        out = t.score(GOLDEN["jobs"][case["job"]], GOLDEN["profiles"][case["profile"]], today)
        if case["score_raw"] is None:
            assert out is None
            continue
        score, reasoning, report = out
        assert score == round(min(100, max(0, case["score_raw"])), 1)
        want = "Great" if score >= b["great"] else "Good" if score >= b["good"] else "Possible" if score >= b["possible"] else "Poor"
        prefix = "Quick match (preview only)" if case.get("preview") else "Quick match"
        assert report["bucket"] == want and reasoning.startswith(f"{prefix}: {want} fit")
        assert report.get("preview", False) == case.get("preview", False)
        met = [ln for ln, p in zip(case["lines"], case["p"], strict=True) if p > 0.5]
        assert sorted(report["matched_skills"]) == sorted(met)
        assert sorted(report["missing_skills"]) == sorted(set(case["lines"]) - set(met))
    assert any(c["score_raw"] and c["score_raw"] > 100 for c in GOLDEN["cases"])  # the clamp is exercised
    assert any(c.get("preview") for c in GOLDEN["cases"])  # preview-only scoring is exercised


def test_preview_lines_when_there_are_no_requirement_lines():
    d = GOLDEN["jobs"]["preview"]["description"]
    assert TR.req_lines(d) == []
    lines, preview = TR.job_lines(d)
    assert preview and len(lines) == 4 and all(25 <= len(x) <= 300 for x in lines)
    assert TR.job_lines("Make great coffee. Smile a lot.") == (["Make great coffee. Smile a lot."], True)  # whole text
    assert TR.job_lines("x" * 400) == (["x" * 300], True)  # whole text, first 300 chars
    assert TR.job_lines("• One two three four five six seven\n" * 30)[0] == ["One two three four five six seven"] * 20
    bi = GOLDEN["jobs"]["bi"]["description"]
    assert TR.job_lines(bi) == (TR.req_lines(bi), False)  # requirement lines win, unmarked
    for empty in ("", "   \n ", "Now hiring!", None):
        assert TR.job_lines(empty) == ([], False)


def test_embeddings_are_cached_per_text():
    calls = []
    t = TR.Triage(GOLDEN["data"], lambda texts: calls.append(list(texts)) or lookup(texts))
    prof, job = GOLDEN["profiles"]["analyst"], GOLDEN["jobs"]["bi"]
    t.score(job, prof)
    n = sum(map(len, calls))
    t.score(job, prof)
    assert sum(map(len, calls)) == n  # second run: nothing re-embedded
    t.score(GOLDEN["jobs"]["sre"], prof)
    assert not set(TR.chunks(prof)) & set(sum(calls[2:], []))  # a new job embeds only its own lines


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _quick_ready(monkeypatch, triage=None):
    monkeypatch.setattr(TM, "installed", lambda: True)
    monkeypatch.setattr(TR, "get", lambda: triage or golden_triage())


def _llm(monkeypatch, calls):
    async def fake(job, profile, config):
        calls.append(job["title"])
        return 42.0, "LLM", {"matched_skills": ["x"], "missing_skills": [], "keywords": []}
    monkeypatch.setattr(ai_engine, "_score_job_fit_llm", fake)


def test_local_match_model_scores_with_quick_match_and_no_llm(monkeypatch):
    _quick_ready(monkeypatch)
    calls = []
    _llm(monkeypatch, calls)
    score, reasoning, report = asyncio.run(ai_engine.score_job_fit(GOLDEN["jobs"]["sre"], GOLDEN["profiles"]["devops"], QUICK))
    assert calls == [] and reasoning.startswith("Quick match")
    assert report["scored_by"] == "triage" and report["bucket"] in TR.BUCKETS and report["score_seconds"] >= 0
    assert set(report) >= {"matched_skills", "missing_skills", "keywords"}


def test_nothing_to_judge_goes_to_the_llm(monkeypatch):
    _quick_ready(monkeypatch)
    calls = []
    _llm(monkeypatch, calls)
    assert asyncio.run(ai_engine.score_job_fit(GOLDEN["jobs"]["blank"], GOLDEN["profiles"]["devops"], QUICK))[0] == 42.0
    assert calls == ["Barista"]


def test_a_preview_is_scored_by_quick_match_and_marked(monkeypatch):
    _quick_ready(monkeypatch)
    calls = []
    _llm(monkeypatch, calls)
    _, reasoning, report = asyncio.run(ai_engine.score_job_fit(GOLDEN["jobs"]["preview"], GOLDEN["profiles"]["devops"], QUICK))
    assert calls == [] and reasoning.startswith("Quick match (preview only): ")
    assert report["scored_by"] == "triage" and report["preview"] is True and report["bucket"] in TR.BUCKETS
    _, _, full = asyncio.run(ai_engine.score_job_fit(GOLDEN["jobs"]["sre"], GOLDEN["profiles"]["devops"], QUICK))
    assert "preview" not in full


def test_not_downloaded_starts_the_download_and_uses_the_llm(monkeypatch):
    started = []
    monkeypatch.setattr(TM, "installed", lambda: False)
    monkeypatch.setattr(TM, "install", lambda: started.append(1))
    calls = []
    _llm(monkeypatch, calls)
    assert asyncio.run(ai_engine.score_job_fit(GOLDEN["jobs"]["sre"], GOLDEN["profiles"]["devops"], QUICK))[0] == 42.0
    assert started and calls


def test_other_tiers_never_touch_quick_match(monkeypatch):
    monkeypatch.setattr(TM, "installed", lambda: pytest.fail("Quick match consulted"))
    calls = []
    _llm(monkeypatch, calls)
    for tier in ("strong", "fast", "utility", None):
        cfg = {"ai": {"scoring_tier": tier} if tier else {}}
        asyncio.run(ai_engine.score_job_fit(GOLDEN["jobs"]["sre"], GOLDEN["profiles"]["devops"], cfg))
    assert len(calls) == 4


def test_the_llm_fallback_uses_the_content_tier(monkeypatch):
    seen = []

    async def client(config, tier="strong"):
        seen.append(tier)
        raise ai_engine.ScoringUnavailable("stop here")
    monkeypatch.setattr(ai_engine, "get_client", client)
    with pytest.raises(ai_engine.ScoringUnavailable):
        asyncio.run(ai_engine._score_job_fit_llm(GOLDEN["jobs"]["sre"], {}, QUICK))
    assert seen == ["strong"]


class FixedNLI:
    def entail(self, pairs):
        return [0.9] * len(pairs)


def _refine(monkeypatch, cfg, scorer):
    monkeypatch.setattr(nli, "get_scorer", lambda c: scorer)
    scored = [({"title": f"job{i}", "description": GOLDEN["jobs"]["sre"]["description"]}, float(i)) for i in range(20)]

    async def run():
        return [x async for x in ai_engine.refine_top_matches(scored, GOLDEN["profiles"]["devops"], cfg)]
    return asyncio.run(run())


def test_refine_rescores_the_top_share_with_the_detailed_model(monkeypatch):
    out = _refine(monkeypatch, {"ai": {**QUICK["ai"], "triage_refine": True, "nli_beta": {"enabled": True}}}, FixedNLI())
    assert [j["title"] for j, *_ in out] == ["job19", "job18", "job17"]  # ceil(15% of 20)
    assert all(r["scored_by"] == "local_model" and s == pytest.approx(90) for _, s, _, r in out)


def test_refine_skips_preview_only_jobs(monkeypatch):
    monkeypatch.setattr(nli, "get_scorer", lambda c: FixedNLI())
    prev = GOLDEN["jobs"]["preview"]["description"]
    scored = [({"title": f"p{i}", "description": prev}, 99.0) for i in range(10)]
    scored += [({"title": f"job{i}", "description": GOLDEN["jobs"]["sre"]["description"]}, float(i)) for i in range(20)]
    cfg = {"ai": {**QUICK["ai"], "triage_refine": True, "nli_beta": {"enabled": True}}}

    async def run():
        return [x async for x in ai_engine.refine_top_matches(scored, GOLDEN["profiles"]["devops"], cfg)]
    assert [j["title"] for j, *_ in asyncio.run(run())] == ["job19", "job18", "job17"]


@pytest.mark.parametrize("refine,scorer", [(False, FixedNLI()), (True, None)])
def test_refine_is_off_by_default_and_needs_the_detailed_model(monkeypatch, refine, scorer):
    assert _refine(monkeypatch, {"ai": {**QUICK["ai"], "triage_refine": refine}}, scorer) == []


# ---------------------------------------------------------------------------
# Download (local server) + the shipped pins
# ---------------------------------------------------------------------------

def test_shipped_download_location_is_the_triage_release():
    src = Path(TM.__file__).read_text()
    assert 'DEFAULT_BASE_URL = "https://github.com/TheDevRo/Jobsmith/releases/download/triage-model-v1"' in src


@pytest.fixture
def triage_server(tmp_path, monkeypatch):
    import http.server
    files = {name: name.encode() * 1000 for name in TM.FILES}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = files.get(self.path.lstrip("/"))
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    monkeypatch.setattr(TM, "FILES", {n: (len(b), hashlib.sha256(b).hexdigest()) for n, b in files.items()})
    monkeypatch.setattr(TM, "SIZE_BYTES", sum(map(len, files.values())))
    monkeypatch.setenv("JOBSMITH_HOME", str(tmp_path))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("JOBSMITH_TRIAGE_MODEL_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    yield files
    srv.shutdown()


def _wait():
    t = time.time()
    while TM.status()["state"] == "downloading" and time.time() - t < 20:
        time.sleep(0.02)
    return TM.status()


def test_download_install_delete(triage_server):
    assert TM.status()["state"] == "not_installed"
    TM.install()
    assert _wait() == {"state": "ready", "progress": 1.0, "size_bytes": TM.SIZE_BYTES, "error": None}
    assert sorted(p.name for p in TM.model_dir().iterdir()) == sorted(TM.FILES)
    assert TM.delete()["state"] == "not_installed" and not TM.model_dir().exists()


def test_checksum_mismatch_is_rejected(triage_server, monkeypatch):
    monkeypatch.setitem(TM.FILES, TM.DATA_FILE, (TM.FILES[TM.DATA_FILE][0], "0" * 64))
    TM.install()
    s = _wait()
    assert s["state"] == "error" and "checksum" in s["error"] and not TM.installed()


def test_status_api(triage_server, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.routers import settings as settings_router
    app = FastAPI()
    app.include_router(settings_router.router)
    c = TestClient(app)
    assert c.get("/api/ai/triage/status").json()["state"] == "not_installed"
    assert c.post("/api/ai/triage/install").json()["state"] in ("downloading", "ready")
    _wait()
    assert c.get("/api/ai/triage/status").json()["state"] == "ready"
    assert c.delete("/api/ai/triage/model").json()["state"] == "not_installed"
