"""Local AI model (beta): switch gating, Apply Assist pass-4 routing, scoring fallback,
the no-hallucination invariant over the extractive gold set, and the model download flow.

No model is needed: a fake NLIScorer stands in for onnxruntime everywhere except the
off-mode subprocess check, which proves onnxruntime is never imported when the switch is off.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.server
import random
import subprocess
import sys
import textwrap
import threading
import time
from datetime import date
from pathlib import Path

import pytest
import yaml

from backend import ai_engine, nli
from backend.auto_apply import answer_bank as ab
from backend.auto_apply import extractive as X
from backend.auto_apply.extractive import handlers as H
from backend.auto_apply.extractive.facts import build_facts, years_with
from backend.auto_apply.field_matcher import match_profile_fields
from backend.auto_apply.llm_client import LLMClient
from backend.auto_apply.models import FieldDescriptor, FieldValue, JobApplicationRequest, UserProfile
from backend.nli import fit
from backend.nli import model as M

ROOT = Path(__file__).resolve().parent.parent
GOLD = Path(__file__).parent / "fixtures" / "extractive_gold"


@pytest.fixture(autouse=True)
def _no_real_model_downloads(monkeypatch):
    """Tests never hit the hosted model (690 MB): the default download location is blanked; tests that
    download use the local model_server fixture via JOBSMITH_NLI_MODEL_URL."""
    from backend.nli import model as M
    monkeypatch.setattr(M, "DEFAULT_BASE_URL", "")


def test_shipped_download_location_is_the_model_release():
    import importlib
    from backend.nli import model as M
    src = importlib.util.find_spec(M.__name__).origin
    assert 'DEFAULT_BASE_URL = "https://github.com/TheDevRo/Jobsmith/releases/download/nli-model-v1"' in open(src).read()
TODAY = date(2026, 9, 26)  # the gold set's reference date
ON = {"ai": {"base_url": "http://mock.invalid/v1", "nli_beta": {"enabled": True}}}
OFF = {"ai": {"base_url": "http://mock.invalid/v1"}}


class FakeNLI:
    """Deterministic pseudo-random scores (or a fixed entailment): the invariant must hold whatever it says."""

    def __init__(self, seed=0, fixed=None):
        self.rng, self.fixed, self.calls = random.Random(seed), fixed, 0

    def probs(self, pairs):
        self.calls += len(pairs)
        out = []
        for _ in pairs:
            e = self.fixed if self.fixed is not None else self.rng.random()
            c = (1 - e) * self.rng.random()
            out.append([e, 1 - e - c, c])
        return out

    def entail(self, pairs):
        return [p[0] for p in self.probs(pairs)]


class BoomNLI:
    def entail(self, pairs):
        raise RuntimeError("onnx exploded")

    probs = entail


@pytest.fixture(autouse=True)
def _isolated_bank(tmp_path, monkeypatch):
    """Pass 3 must not see a real answer bank."""
    monkeypatch.setattr(ab, "_instance", ab.AnswerBank(tmp_path / "answer_bank.json"))


@pytest.fixture(autouse=True)
def _pinned_today(monkeypatch):
    real = X.fill

    async def fill(*a, **kw):
        kw.setdefault("today", TODAY)
        return await real(*a, **kw)
    monkeypatch.setattr(X, "fill", fill)


def use_scorer(monkeypatch, scorer):
    monkeypatch.setattr(nli, "get_scorer", lambda cfg: scorer if nli.enabled(cfg) else None)


def gold():
    profiles = {k: UserProfile.from_config({"profile": v})
                for k, v in yaml.safe_load((GOLD / "profiles.yaml").read_text()).items()}
    forms = []
    for f in yaml.safe_load((GOLD / "questions.yaml").read_text())["forms"]:
        fields = [FieldDescriptor(field_id=f"{f['id'][:2]}_q{i + 1:02d}", **q["field"]) for i, q in enumerate(f["questions"])]
        forms.append((JobApplicationRequest(**f["job"]), fields, [q["kind"] for q in f["questions"]]))
    return profiles, forms


def run(client, profile, job, fields):
    return asyncio.run(client.map_fields_to_values(profile, job, fields, {}))


def check_invariant(profile, fields, values):
    """Every pass-4, non-essay fill is a form option, a verbatim profile fact, or a date computation."""
    facts = {f.value for f in build_facts(profile, {}, TODAY)}
    det = match_profile_fields(profile, fields)
    by_id = {f.field_id: f for f in fields}
    bad = []
    for v in values:
        f = by_id[v.field_id]
        if not v.value or v.field_id in det or (f.field_type or "") == "file" or v.source == "llm_generated":
            continue
        if f.options:
            ok = v.value in f.options
        elif v.value in facts:
            ok = True
        else:
            yrs = years_with(profile, H.skill_terms(f.label or ""), TODAY)
            ok = yrs is not None and v.value == str(int(yrs))
        if not ok:
            bad.append((f.label, v.value, v.source))
    return bad


class LLMStub(LLMClient):
    """Counts LLM traffic; complete_json answers every field with a marker value."""

    def __init__(self, cfg, essay='["I am excited about this role."]'):
        super().__init__(cfg)
        self.json_calls, self.essay_calls, self.essay_text, self.essay_profiles = 0, 0, essay, []

    async def complete_json(self, system, user, max_retries=3):
        import json
        self.json_calls += 1
        fields = json.loads(user[user.index("FORM FIELDS TO MAP:\n") + 20:user.rindex("\n\nReturn the JSON array now.")])
        return [{"field_id": f["field_id"], "value": "LLM", "action": "fill", "confidence": 0.9,
                 "source": "llm_generated"} for f in fields]

    async def generate_answer(self, question, profile, job, max_words=80):
        self.essay_calls += 1
        self.essay_profiles.append(profile)
        if isinstance(self.essay_text, Exception):
            raise self.essay_text
        return self.essay_text


# ---------------------------------------------------------------------------
# Config + sync
# ---------------------------------------------------------------------------

def test_switch_defaults_off():
    example = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    assert example["ai"]["nli_beta"] == {"enabled": False}
    assert not nli.enabled({}) and not nli.enabled({"ai": {}}) and not nli.enabled({"ai": {"nli_beta": None}})
    assert nli.enabled(ON)
    assert nli.get_scorer(OFF) is None


def test_switch_never_syncs():
    from backend.sync import settings_registry as R
    row = next(s for s in R.REGISTRY if s.canonical == "ai.nli_beta.enabled")
    assert row.cls == R.Cls.LOCAL
    assert not any(k.startswith("ai.nli_beta") for k in R.syncable_canonical_ids())
    cfg = {**ON, "sync": {"settings": {c: True for c in R.CATEGORY_KEYS}}}
    assert not any(k.startswith("ai.nli_beta") for k in R.export_settings(cfg))


# ---------------------------------------------------------------------------
# Off mode: byte-for-byte today's behaviour, onnxruntime never imported
# ---------------------------------------------------------------------------

OFF_SCRIPT = textwrap.dedent("""
    import asyncio, sys, json
    from backend import ai_engine
    from backend.auto_apply import answer_bank as ab
    from backend.auto_apply.llm_client import LLMClient
    from backend.auto_apply.models import FieldDescriptor, JobApplicationRequest, UserProfile
    ab._instance = ab.AnswerBank(sys.argv[1])
    class Stub(LLMClient):
        async def complete_json(self, system, user, max_retries=3):
            return [{"field_id": "q1", "value": "Yes", "action": "select", "confidence": 0.9, "source": "llm_generated"}]
    fields = [FieldDescriptor(field_id="q1", label="Are you willing to travel?", field_type="select", options=["Yes", "No"])]
    job = JobApplicationRequest(job_id="1", title="Engineer", company="Acme", url="https://x.invalid")
    profile = UserProfile(full_name="A B", email="a@b.c")
    out = []
    for cfg in ({"ai": {}}, {"ai": {"nli_beta": {"enabled": False}}}):
        vals = asyncio.run(Stub(cfg).map_fields_to_values(profile, job, fields, {}))
        out.append([v.model_dump() for v in vals])
        async def boom(*a, **k): raise ai_engine.ScoringUnavailable("down")
        ai_engine._score_job_fit_llm = boom
        try:
            asyncio.run(ai_engine.score_job_fit({"title": "t", "description": "5 years of Python experience required."}, {}, cfg))
            out.append("scored")
        except ai_engine.ScoringUnavailable:
            out.append("unavailable")
    loaded = [m for m in sys.modules if m.split(".")[0] in ("onnxruntime", "tokenizers", "numpy")
              or m.startswith("backend.nli.") or m.startswith("backend.auto_apply.extractive")]
    print(json.dumps({"out": out, "loaded": loaded}))
""")


def test_off_mode_never_imports_the_runtime(tmp_path):
    import json
    r = subprocess.run([sys.executable, "-c", OFF_SCRIPT, str(tmp_path / "bank.json")], cwd=ROOT,
                       capture_output=True, text=True, env={"JOBSMITH_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res["loaded"] == [], res["loaded"]
    fv = {"field_id": "q1", "value": "Yes", "action": "select", "confidence": 0.9, "source": "llm_generated"}
    assert res["out"] == [[fv], "unavailable", [fv], "unavailable"]


def test_off_mode_is_the_llm_path(monkeypatch):
    use_scorer(monkeypatch, FakeNLI(fixed=1.0))
    profiles, forms = gold()
    job, fields, _ = forms[0]
    a, b = LLMStub(OFF), LLMStub({"ai": {**OFF["ai"], "nli_beta": {"enabled": False}}})
    assert run(a, profiles["csm"], job, fields) == run(b, profiles["csm"], job, fields)
    assert a.json_calls == 1 and a.essay_calls == 0


# ---------------------------------------------------------------------------
# On mode: routing, essays, fallbacks
# ---------------------------------------------------------------------------

def test_on_and_ready_skips_the_field_map_llm(monkeypatch):
    fake = FakeNLI(fixed=1.0)
    use_scorer(monkeypatch, fake)
    profiles, forms = gold()
    job, fields, kinds = forms[0]
    c = LLMStub(ON)
    values = run(c, profiles["csm"], job, fields)
    assert c.json_calls == 0 and fake.calls > 0
    assert [v.field_id for v in values] == [f.field_id for f in fields]
    assert all(v.value != "LLM" for v in values)
    assert {v.source for v in values} <= {"profile", "answer_bank", "llm_generated", "skip"}
    essays = [v for v, k in zip(values, kinds, strict=True) if k == "essay"]
    assert essays and all(v.source == "llm_generated" and v.confidence == 0.5 for v in essays)
    assert all(v.value == "I am excited about this role." for v in essays)  # JSON-wrapped output unwrapped
    # EEO answers never reach an essay prompt
    assert all(not (p.gender or p.race_ethnicity or p.veteran_status or p.disability_status) for p in c.essay_profiles)


def test_essay_refusal_or_outage_is_left_for_the_user(monkeypatch):
    use_scorer(monkeypatch, FakeNLI(0))
    profiles, forms = gold()
    job, fields, kinds = forms[0]
    for essay in ("I cannot answer this based on the profile.", RuntimeError("LM Studio down")):
        values = run(LLMStub(ON, essay=essay), profiles["csm"], job, fields)
        essays = [v for v, k in zip(values, kinds, strict=True) if k == "essay"]
        assert essays and all(v.value == "" and v.action == "skip" for v in essays)


def test_on_but_model_missing_uses_the_llm(monkeypatch):
    use_scorer(monkeypatch, None)
    profiles, forms = gold()
    job, fields, _ = forms[0]
    c = LLMStub(ON)
    values = run(c, profiles["csm"], job, fields)
    assert c.json_calls == 1 and any(v.value == "LLM" for v in values)


def test_nli_runtime_error_falls_back_to_the_llm(monkeypatch):
    use_scorer(monkeypatch, BoomNLI())
    profiles, forms = gold()
    job, fields, _ = forms[0]
    c, ref = LLMStub(ON), LLMStub(OFF)
    assert run(c, profiles["csm"], job, fields) == run(ref, profiles["csm"], job, fields)
    assert c.json_calls == 1


# ---------------------------------------------------------------------------
# No-hallucination invariant over the prototype's gold set (6 profiles x 6 forms)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scorer", [FakeNLI(0), FakeNLI(1), FakeNLI(2), FakeNLI(fixed=1.0)],
                         ids=["seed0", "seed1", "seed2", "always-entailed"])
def test_no_hallucination_invariant(monkeypatch, scorer):
    use_scorer(monkeypatch, scorer)
    profiles, forms = gold()
    filled = 0
    for pk, profile in profiles.items():
        for job, fields, kinds in forms:
            values = run(LLMStub(ON), profile, job, fields)
            assert check_invariant(profile, fields, values) == [], (pk, job.job_id)
            filled += sum(bool(v.value) and v.source == "profile" for v in values)
            if pk != "sales":  # only the sales profile declines EEO questions
                for v, k in zip(values, kinds, strict=True):
                    if k == "eeo" and v.value and v.field_id not in match_profile_fields(profile, fields):
                        assert not any(w in v.value.lower() for w in ("decline", "wish", "want to answer", "prefer not"))
    assert filled > 50  # the scorer does make pass 4 fill things


def test_invariant_check_catches_an_invented_value():
    profiles, forms = gold()
    job, fields, _ = forms[0]
    f = next(f for f in fields if not f.options and (f.field_type or "text") == "text"
             and f.field_id not in match_profile_fields(profiles["csm"], fields))
    bad = [FieldValue(field_id=f.field_id, value="Invented Corp", action="fill", source="profile")]
    assert check_invariant(profiles["csm"], fields, bad)


# ---------------------------------------------------------------------------
# Scoring fallback
# ---------------------------------------------------------------------------

JOB = {"title": "Data Engineer", "description": "About us: we ship data.\n"
       "- 5+ years of experience with Python and SQL\n- Bachelor's degree in Computer Science or similar\n"
       "- Experience with Airflow is a plus\nWe offer great benefits and snacks."}
PROFILE = {"summary": "Data engineer.", "skills": ["Python", "SQL"],
           "experience": [{"title": "Data Engineer", "company": "Acme", "start_date": "2018-01", "end_date": ""}],
           "education": [{"degree": "BS Computer Science", "school": "State U"}], "certifications": []}


def _llm_down(monkeypatch, exc=None):
    async def boom(*a, **k):
        raise exc or ai_engine.ScoringUnavailable("AI error: connection refused")
    monkeypatch.setattr(ai_engine, "_score_job_fit_llm", boom)


def test_scoring_falls_back_to_local_model(monkeypatch):
    _llm_down(monkeypatch)

    class Half(FakeNLI):
        def entail(self, pairs):
            return [0.9 if "Python" in h or "Bachelor" in h else 0.1 for _, h in pairs]
    use_scorer(monkeypatch, Half())
    score, reasoning, report = asyncio.run(ai_engine.score_job_fit(JOB, PROFILE, ON))
    assert reasoning.startswith("Scored by the local model (beta)")
    assert score == pytest.approx(100 * (0.9 + 0.9 + 0.1) / 3, abs=0.1)
    assert set(report) == {"matched_skills", "missing_skills", "matched_soft_skills", "missing_soft_skills",
                           "keywords", "title_alignment"}
    assert report["matched_skills"] == ["5+ years of experience with Python and SQL",
                                        "Bachelor's degree in Computer Science or similar"]
    assert report["missing_skills"] == ["Experience with Airflow is a plus"]


def test_scoring_fallback_also_covers_a_dead_on_device_bridge(monkeypatch):
    from backend import apple_bridge
    _llm_down(monkeypatch, apple_bridge.BridgeUnavailable("helper not running"))
    use_scorer(monkeypatch, FakeNLI(fixed=0.8))
    assert asyncio.run(ai_engine.score_job_fit(JOB, PROFILE, ON))[0] == pytest.approx(80)


@pytest.mark.parametrize("cfg,scorer,job", [
    (OFF, FakeNLI(fixed=1.0), JOB),                     # switch off
    (ON, None, JOB),                                    # model not installed
    (ON, BoomNLI(), JOB),                               # runtime error
    (ON, FakeNLI(fixed=1.0), {"title": "x", "description": "We are nice. Great snacks."}),  # nothing to judge
])
def test_scoring_stays_unavailable(monkeypatch, cfg, scorer, job):
    _llm_down(monkeypatch)
    use_scorer(monkeypatch, scorer)
    with pytest.raises(ai_engine.ScoringUnavailable):
        asyncio.run(ai_engine.score_job_fit(job, PROFILE, cfg))


def test_long_profiles_are_scored_in_pieces():
    seen = []

    class Long(FakeNLI):
        def count_tokens(self, p, h):
            return 900

        def entail(self, pairs):
            seen.extend(p for p, _ in pairs)
            return [0.9 if p.startswith("The candidate worked as") else 0.2 for p, _ in pairs]
    score, _, report = fit.score(JOB, PROFILE, Long())
    whole, *pieces = fit.profile_pieces(PROFILE)
    assert whole not in seen and set(seen) == set(pieces)
    assert score == pytest.approx(90)  # best piece per line
    assert len(report["matched_skills"]) == 3


def test_req_lines_matches_the_bench():
    assert fit.req_lines(JOB["description"]) == [
        "5+ years of experience with Python and SQL", "Bachelor's degree in Computer Science or similar",
        "Experience with Airflow is a plus"]


# ---------------------------------------------------------------------------
# Model download: install, progress, checksum, delete, re-install, network failure, resume
# ---------------------------------------------------------------------------

class _Files(http.server.BaseHTTPRequestHandler):
    files: dict = {}
    fail_after: int | None = None  # drop the connection after this many bytes of the model
    ranges: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = self.files.get(self.path.lstrip("/"))
        if body is None:
            self.send_error(404)
            return
        start = 0
        if rng := self.headers.get("Range"):
            start = int(rng.split("=")[1].split("-")[0])
            self.ranges.append(start)
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(body) - start))
        self.end_headers()
        chunk = body[start:]
        if self.fail_after is not None and self.path.endswith(".onnx"):
            self.wfile.write(chunk[:self.fail_after])
            self.wfile.flush()
            self.connection.shutdown(2)
            return
        self.wfile.write(chunk)


@pytest.fixture
def model_server(tmp_path, monkeypatch):
    model, tok = b"M" * 300_000, b'{"tok": 1}'
    _Files.files = {"model.onnx": model, "tokenizer.json": tok}
    _Files.fail_after, _Files.ranges = None, []
    monkeypatch.setattr(M, "FILES", {"model.onnx": (len(model), hashlib.sha256(model).hexdigest()),
                                     "tokenizer.json": (len(tok), hashlib.sha256(tok).hexdigest())})
    monkeypatch.setattr(M, "SIZE_BYTES", len(model) + len(tok))
    monkeypatch.setenv("JOBSMITH_HOME", str(tmp_path))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Files)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("JOBSMITH_NLI_MODEL_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    yield _Files
    srv.shutdown()


def _wait():
    t = time.time()
    while M.status()["state"] == "downloading" and time.time() - t < 20:
        time.sleep(0.02)
    return M.status()


def test_download_install_delete_reinstall(model_server):
    assert M.status()["state"] == "not_installed"
    assert M.install()["state"] in ("downloading", "ready")
    s = _wait()
    assert s == {"state": "ready", "progress": 1.0, "size_bytes": M.SIZE_BYTES, "error": None}
    d = M.model_dir()
    assert sorted(p.name for p in d.iterdir()) == ["model.onnx", "tokenizer.json"]
    assert M.delete()["state"] == "not_installed" and not d.exists()
    M.install()
    assert _wait()["state"] == "ready"


def test_checksum_mismatch_is_rejected(model_server, monkeypatch):
    monkeypatch.setitem(M.FILES, "model.onnx", (M.FILES["model.onnx"][0], "0" * 64))
    M.install()
    s = _wait()
    assert s["state"] == "error" and "checksum" in s["error"]
    assert not (M.model_dir() / "model.onnx").exists() and not list(M.model_dir().glob("*.part"))
    assert not M.installed()


def test_network_failure_leaves_no_model_then_resumes(model_server):
    model_server.fail_after = 100_000
    M.install()
    s = _wait()
    assert s["state"] == "error" and s["error"]
    assert not (M.model_dir() / "model.onnx").exists() and not M.installed()
    assert nli.get_scorer(ON) is None  # nothing half-installed is ever loaded
    model_server.fail_after = None
    M.install()  # the Retry button
    assert _wait()["state"] == "ready"
    assert model_server.ranges and model_server.ranges[0] >= 100_000  # resumed, not restarted
    assert (M.model_dir() / "model.onnx").read_bytes() == model_server.files["model.onnx"]


def test_no_download_location_is_a_clear_retryable_error(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSMITH_HOME", str(tmp_path))
    monkeypatch.delenv("JOBSMITH_NLI_MODEL_URL", raising=False)
    M.install()
    s = _wait()
    assert s["state"] == "error" and s["error"] == M.NOT_HOSTED and not M.installed()


def test_status_api(model_server, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend import app_state as state
    from backend.routers import settings as settings_router
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.dump({"ai": {"base_url": "http://x.invalid/v1"}}))
    monkeypatch.setattr(state, "CONFIG_PATH", cfg)
    app = FastAPI()
    app.include_router(settings_router.router)
    c = TestClient(app)
    s = c.get("/api/ai/nli/status").json()
    assert s["enabled"] is False and s["state"] == "off" and s["installed"] is False
    s = c.put("/api/settings/nli-beta", json={"enabled": True}).json()  # turning it on starts the install
    assert s["enabled"] is True and s["state"] in ("downloading", "ready")
    assert yaml.safe_load(cfg.read_text())["ai"] == {"base_url": "http://x.invalid/v1", "nli_beta": {"enabled": True}}
    _wait()
    assert c.get("/api/ai/nli/status").json()["state"] == "ready"
    s = c.put("/api/settings/nli-beta", json={"enabled": False}).json()
    assert s["state"] == "off" and s["installed"] is True
    s = c.delete("/api/ai/nli/model").json()
    assert s["installed"] is False
    assert c.post("/api/ai/nli/install").json()["enabled"] is False
    _wait()
