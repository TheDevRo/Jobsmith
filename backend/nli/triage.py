"""Quick match: job-fit triage from a small sentence-embedding model, no LLM and no NLI (fast enough for every job).

The bench method (laya-bench triage_eval.py, `line` pool), ported 1:1 and pinned by a golden test shared with the
Swift twin (TriageFit.swift): each requirement line of the posting (fit.req_lines) gets P(met) from a logistic on
  max / top3 : best and top-3 mean cosine to the profile's chunks (skills, roles, bullets, summary sentences, ...)
  rel        : max minus the best match among reference people (synthetic fixture profiles): "better than anyone"
  soft       : max cosine to generic soft-skill prototypes (the gold counts those as met for anyone)
  skill      : the line names one of the profile's skills (or an alias)
  has_years / years_ratio : an "N+ years" line, and the profile's years with the line's skills / N (capped at 1)
Job score = weights . [mean P(met), share of lines naming a skill, matched skills / min(8, #skills)] + intercept,
clamped to 0-100, then a bucket. Weights, thresholds, aliases and the prototype/reference embeddings come from the
downloaded data file (triage-data.json), so another embedding model is a data swap, not a code change.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date
from typing import Callable

import numpy as np

from .fit import req_lines

logger = logging.getLogger(__name__)

REASONING = "Quick match"
SCORED_BY = "triage"
BUCKETS = ("Poor", "Possible", "Good", "Great")
YEARS = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*\d{1,2}\s*)?\+?\s*years?", re.I)
LINE_COLS = ("max", "top3", "rel", "soft", "skill", "has_years", "years_ratio")
JOB_COLS = ("cov_line", "skill_lines", "skill_count", "title")


def chunks(profile: dict) -> list[str]:
    """Profile text units, each embedded on its own."""
    out = [str(s) for s in profile.get("skills") or []]
    for r in profile.get("experience") or []:
        if r.get("title"):
            out.append(f"{r['title']} at {r.get('company', '') or ''}")
        out += [str(b) for b in r.get("bullets") or [] if b]
    out += [u.strip() for u in re.split(r"(?<=[.!?])\s+", profile.get("summary") or "") if len(u.strip()) > 2]
    out += [f"{e.get('degree', '')} {e.get('school', '')}".strip() for e in profile.get("education") or [] if e.get("degree")]
    out += [str(c) for c in profile.get("certifications") or []]
    return out


def skill_terms(profile: dict, aliases: dict) -> list[tuple[str, list[str]]]:
    """[(skill, [search terms])]; terms shorter than 2 chars are dropped (single letters hit everything)."""
    out = []
    for s in profile.get("skills") or []:
        s = str(s).strip()
        terms = [t for t in [s.lower(), *aliases.get(s.lower(), [])] if len(t) >= 2]
        if terms:
            out.append((s, terms))
    return out


def has(text: str, term: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9+#])", text) is not None


def _years_with(profile: dict, skills: list[str] | None, today: date) -> float | None:
    from ..auto_apply.extractive import facts
    from ..auto_apply.models import UserProfile
    try:
        up = UserProfile.from_config({"profile": profile})
    except Exception:  # noqa: BLE001 — an incomplete profile just has no usable years
        return None
    return facts.years_with(up, skills, today)


class Triage:
    """Scores jobs against a profile. embed(texts) -> unit vectors [n, dim] (the model has pooling + norm inside).
    Embeddings are cached per text: the profile's chunks once per profile, each job's lines and title once."""

    def __init__(self, data: dict, embed: Callable[[list[str]], np.ndarray]):
        for c in data["job"]["cols"]:
            if c not in JOB_COLS:
                raise ValueError(f"triage data: unknown job feature {c!r}")
        self.d, self.embed = data, embed
        self.soft = np.asarray(data["soft"], dtype=np.float64)
        self.ref = np.asarray(data["ref"], dtype=np.float64)
        self.line_ix = [LINE_COLS.index(c) for c in data["line"]["cols"]]
        self._vec: dict[str, np.ndarray] = {}
        self._lock = threading.Lock()

    def vectors(self, texts: list[str]) -> np.ndarray:
        with self._lock:
            todo = [t for t in dict.fromkeys(texts) if t not in self._vec]
            if todo:
                if len(self._vec) > 50_000:  # ponytail: wholesale reset, an LRU if long sessions ever need one
                    self._vec.clear()
                self._vec.update(zip(todo, np.asarray(self.embed(todo), dtype=np.float64), strict=True))
            return np.stack([self._vec[t] for t in texts]) if texts else np.zeros((0, self.ref.shape[1]))

    def line_features(self, profile: dict, lines: list[str], today: date) -> np.ndarray:
        """[lines x LINE_COLS] (bench triage_eval.features `_lf`)."""
        C, Lv = self.vectors(chunks(profile)), self.vectors(lines)
        top = -np.sort(-(Lv @ C.T), axis=1)
        sk = skill_terms(profile, self.d["aliases"])
        rows = []
        for i, line in enumerate(lines):
            low = line.lower()
            hit = [s for s, ts in sk if any(has(low, t) for t in ts)]
            m = YEARS.search(line)
            if m and 1 <= int(m[1]) <= 20:
                y = _years_with(profile, hit or None, today)
                hy, yr = 1.0, min(1.0, (y or 0) / int(m[1]))
            else:
                hy, yr = 0.0, 0.0
            rows.append([top[i, 0], top[i, :3].mean(), top[i, 0] - float((Lv[i] @ self.ref.T).max()),
                         float((Lv[i] @ self.soft.T).max()), float(bool(hit)), hy, yr])
        return np.array(rows, dtype=np.float64)

    def evaluate(self, job: dict, profile: dict, today: date) -> tuple[list[str], np.ndarray, float] | None:
        """(requirement lines, P(met) per line, raw job score) or None when there is nothing to judge."""
        lines = req_lines(job.get("description") or "")
        if not lines or not chunks(profile):
            return None
        lf = self.line_features(profile, lines, today)
        ln = self.d["line"]
        p = 1 / (1 + np.exp(-(lf[:, self.line_ix] @ np.asarray(ln["coef"]) + ln["intercept"])))
        sk = skill_terms(profile, self.d["aliases"])
        desc = (job.get("description") or "").lower()
        feats = {"cov_line": float(p.mean()), "skill_lines": float(lf[:, LINE_COLS.index("skill")].mean()),
                 "skill_count": min(1.0, sum(any(has(desc, t) for t in ts) for _, ts in sk) / min(8, max(1, len(sk))))}
        if "title" in self.d["job"]["cols"]:
            titles = [r["title"] for r in profile.get("experience") or [] if r.get("title")]
            feats["title"] = float((self.vectors([job.get("title") or ""]) @ self.vectors(titles).T).max()) if titles else 0.0
        jw = self.d["job"]
        return lines, p, float(np.dot([feats[c] for c in jw["cols"]], jw["weights"]) + jw["intercept"])

    def score(self, job: dict, profile: dict, today: date | None = None) -> tuple[float, str, dict] | None:
        """(score 0-100, reasoning, report) or None when the posting has no requirement lines or the profile is empty."""
        ev = self.evaluate(job, profile, today or date.today())
        if ev is None:
            return None
        lines, p, raw = ev
        score = round(min(100.0, max(0.0, raw)), 1)
        b = self.d["buckets"]
        bucket = BUCKETS[sum(score >= b[k] for k in ("possible", "good", "great"))]
        order = np.argsort(-p, kind="stable")
        met = [lines[i] for i in order if p[i] > 0.5]
        missing = [lines[i] for i in order[::-1] if p[i] <= 0.5]
        reasoning = f"{REASONING}: {bucket} fit, meets about {len(met)} of {len(lines)} requirement lines."
        return score, reasoning, {"matched_skills": met, "missing_skills": missing, "keywords": [], "bucket": bucket}


class OnnxEmbedder:
    """The downloaded ONNX export: ids (int64, pad 0) -> unit vectors. Imported only when Quick match is used."""

    def __init__(self, model_dir, seq: int):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.tok = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tok.enable_truncation(seq)
        self.tok.enable_padding(pad_id=0, pad_token="[PAD]")
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        from .triage_model import ONNX_FILE
        self.session = ort.InferenceSession(str(model_dir / ONNX_FILE), opts, providers=["CPUExecutionProvider"])

    def __call__(self, texts: list[str]) -> np.ndarray:
        out = []
        for s in range(0, len(texts), 64):
            ids = np.array([e.ids for e in self.tok.encode_batch(texts[s:s + 64])], dtype=np.int64)
            out.append(self.session.run(None, {"ids": ids})[0])
        return np.concatenate(out)


_lock = threading.Lock()
_triage: Triage | None = None


def get() -> Triage | None:
    """The loaded Quick match model (process-wide), or None when it isn't installed or won't load."""
    global _triage
    from . import triage_model as tm
    with _lock:
        if _triage is None and tm.installed():
            try:
                data = json.loads((tm.model_dir() / tm.DATA_FILE).read_text())
                _triage = Triage(data, OnnxEmbedder(tm.model_dir(), data["seq"]))
            except Exception:  # noqa: BLE001 — a broken model must never break the LLM path
                logger.exception("Quick match model failed to load; using the LLM instead")
        return _triage


def unload() -> None:
    global _triage
    with _lock:
        _triage = None
