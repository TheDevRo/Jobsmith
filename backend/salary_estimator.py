"""
salary_estimator.py — Estimate compensation for jobs that lack disclosed salary,
and produce a "vs market" comparison for jobs that have one.

All numbers come from real external data sources (Adzuna histogram primary,
BLS OEWS secondary). The local LLM is only used to canonicalize the job title
and extract a seniority signal so the API queries return tighter matches; it is
NEVER allowed to invent salary numbers.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

from . import ai_engine
from . import database as db

logger = logging.getLogger(__name__)


class QuotaExceeded(Exception):
    """Raised when an external salary data source rejects us for rate-limit reasons.

    The batch worker catches this and stops the run cleanly, preserving whatever
    estimates were already persisted. Avoids burning the daily quota on jobs we
    can't actually estimate right now.
    """


class ResourceExhausted(Exception):
    """Raised when the process has run out of OS resources (file descriptors,
    sockets, etc.). The batch worker stops immediately and tells the user to
    restart the server — no point continuing because every subsequent operation
    will fail the same way until the process is killed and the OS reclaims FDs.
    """


def _is_fd_exhaustion(exc: BaseException) -> bool:
    """Detect Errno 24 ("Too many open files") anywhere in an exception chain.

    The error can surface as OSError, sqlite3.OperationalError, ssl errors, or
    httpx connection errors depending on which subsystem hit the wall first.
    """
    seen = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, OSError) and getattr(cur, "errno", None) == 24:
            return True
        msg = str(cur)
        if "Too many open files" in msg or "out of system resource" in msg:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


# Adzuna client cached for the lifetime of the process so a batch doesn't
# leak one httpx pool per job. The OpenAI client is cached centrally in
# ai_engine._get_client.
_adzuna_http_client: Optional[httpx.AsyncClient] = None


async def _get_cached_adzuna_client() -> httpx.AsyncClient:
    """Lazy single AsyncClient for Adzuna calls — reused, never closed
    explicitly during the batch (the OS reclaims when the process exits)."""
    global _adzuna_http_client
    if _adzuna_http_client is None or getattr(_adzuna_http_client, "is_closed", False):
        _adzuna_http_client = httpx.AsyncClient(timeout=15.0)
    return _adzuna_http_client


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def estimate_salary(job: dict, config: dict) -> Optional[dict]:
    """Estimate annual salary range for a job using real external data.

    Returns a dict with min/max/period/source/confidence/metadata, or None
    if no source could produce a usable estimate.

    Strategy:
      1. Ask the local LLM (fast tier) to canonicalize the job title and
         classify seniority. Cached by (title, description hash).
      2. Look up Adzuna histogram for the canonical title + location.
         Derive p25 and p75 (or the closest available percentiles) as the
         estimated range.
      3. If Adzuna fails, fall back to BLS OEWS by SOC + MSA (when
         configured). The MSA mapping is intentionally small — we only need
         metro accuracy for the user's local market and a "national" bucket
         for everywhere else.
      4. Cache the lookup result by (source-key) for 30 days so re-running
         the estimator across the queue doesn't burn the daily API quota.
    """
    cfg = config.get("salary_estimator", {}) or {}
    if not cfg.get("enabled", True):
        return None

    title = (job.get("title") or "").strip()
    location = (job.get("location") or "").strip()
    if not title:
        return None

    # 1. Canonicalize role (LLM only — no salary data passes through here)
    classification = await classify_job_role(
        title, job.get("description", "") or "", config
    )

    # 2. Adzuna histogram (primary)
    adzuna_cfg = config.get("api_keys", {}) or {}
    app_id = adzuna_cfg.get("adzuna_app_id") or cfg.get("adzuna", {}).get("app_id")
    app_key = adzuna_cfg.get("adzuna_app_key") or cfg.get("adzuna", {}).get("app_key")
    country = (cfg.get("adzuna", {}).get("country") or "us").lower()

    payload: Optional[dict] = None
    if app_id and app_key:
        try:
            payload = await lookup_adzuna_histogram(
                what=classification["canonical_title"],
                where=location,
                country=country,
                app_id=app_id,
                app_key=app_key,
            )
            # Some niche city strings (e.g. "Glendale, Denver") return an
            # empty histogram even though the title is well-represented
            # nationally. Retry without `where` so we still get *some* signal.
            if not payload and location:
                logger.info(
                    "adzuna: no data for %r in %r — retrying without location",
                    classification["canonical_title"], location,
                )
                payload = await lookup_adzuna_histogram(
                    what=classification["canonical_title"],
                    where="",
                    country=country,
                    app_id=app_id,
                    app_key=app_key,
                )
                if payload:
                    payload.setdefault("metadata", {})["location_fallback"] = "national"
                    # National distributions get a confidence bump down — the
                    # number is real but doesn't reflect the local market.
                    if payload.get("confidence") == "high":
                        payload["confidence"] = "medium"
                    elif payload.get("confidence") == "medium":
                        payload["confidence"] = "low"
        except (QuotaExceeded, ResourceExhausted):
            # Bubble up so the batch worker can stop cleanly.
            raise
        except Exception as e:
            if _is_fd_exhaustion(e):
                raise ResourceExhausted(
                    "Process is out of file descriptors — restart the server to recover."
                ) from e
            logger.warning("adzuna lookup failed for %s / %s (%s)", title, location, e)

    # 3. BLS fallback (optional)
    if not payload:
        bls_key = cfg.get("bls", {}).get("api_key") or None
        soc = classification.get("soc_code")
        msa = location_to_msa(location)
        if soc:
            try:
                payload = await asyncio.to_thread(
                    lookup_bls_oews, soc, msa, bls_key
                )
            except Exception:
                logger.exception("BLS OEWS lookup failed for soc=%s msa=%s", soc, msa)

    if not payload:
        return None

    # Apply seniority adjustment when the LLM signals junior/senior. Adzuna's
    # histogram already mixes levels, so a small scalar nudges the range
    # toward where this specific posting sits in that distribution. This is a
    # multiplicative shift on top of real percentiles, NOT an invented number.
    seniority = classification.get("seniority")
    multiplier = _seniority_multiplier(seniority)
    if multiplier != 1.0 and payload.get("min") and payload.get("max"):
        payload["min"] = int(payload["min"] * multiplier)
        payload["max"] = int(payload["max"] * multiplier)
        meta = payload.setdefault("metadata", {})
        meta["seniority_multiplier"] = multiplier
        meta["seniority"] = seniority

    payload.setdefault("metadata", {}).update({
        "canonical_title": classification.get("canonical_title"),
        "soc_code": classification.get("soc_code"),
        "soc_title": classification.get("soc_title"),
    })
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    return payload


def _seniority_multiplier(seniority: Optional[str]) -> float:
    if not seniority:
        return 1.0
    return {
        "intern": 0.55,
        "entry": 0.80,
        "junior": 0.85,
        "mid": 1.00,
        "senior": 1.20,
        "staff": 1.45,
        "principal": 1.70,
        "manager": 1.25,
        "director": 1.55,
    }.get(seniority.lower(), 1.0)


# ---------------------------------------------------------------------------
# LLM role classification (no salary numbers — just canonicalization)
# ---------------------------------------------------------------------------

_SOC_HINTS = """Common SOC examples (use the closest match; reply with the 6-digit code in NN-NNNN form):
- 15-1252 Software Developers
- 15-1244 Network and Computer Systems Administrators
- 15-1212 Information Security Analysts
- 15-1232 Computer User Support Specialists
- 15-1299 Computer Occupations, All Other
- 15-2051 Data Scientists
- 13-1082 Project Management Specialists
- 11-3021 Computer and Information Systems Managers
- 41-3091 Sales Representatives, Services
- 25-1199 Postsecondary Teachers, All Other
"""


async def classify_job_role(title: str, description: str, config: dict) -> dict:
    """Use the fast LLM to canonicalize the role + extract seniority + SOC code.

    Returns: {canonical_title, seniority, soc_code, soc_title}.
    Cached in the salary_lookup_cache table by hash(title|desc) so the same
    posting doesn't hit the LLM twice.
    """
    cache_key = "soc:" + hashlib.sha256(
        (title.lower().strip() + "|" + (description or "")[:1500]).encode("utf-8")
    ).hexdigest()[:24]

    cached = await db.get_salary_cache(cache_key)
    if cached is not None:
        return cached

    prompt = (
        "Classify the job posting below. Return ONLY a JSON object with these keys:\n"
        '  "canonical_title": short generic role name (e.g. "software engineer", "cybersecurity analyst")\n'
        '  "seniority": one of [intern, entry, junior, mid, senior, staff, principal, manager, director]\n'
        '  "soc_code": closest 6-digit SOC code in "NN-NNNN" format\n'
        '  "soc_title": label for that SOC code\n\n'
        f"{_SOC_HINTS}\n"
        f"TITLE: {title}\n"
        f"DESCRIPTION: {(description or '')[:1500]}\n\n"
        "Return only the JSON object."
    )

    result = {
        "canonical_title": _fallback_canonical_title(title),
        "seniority": _fallback_seniority(title),
        "soc_code": None,
        "soc_title": None,
    }
    try:
        client = ai_engine._get_client(config, "utility")
        response = await client.chat.completions.create(
            model=ai_engine._model(config, "utility"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
        )
        text = (response.choices[0].message.content or "").strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", text)
            parsed = json.loads(match.group()) if match else {}

        if isinstance(parsed, dict):
            for key in ("canonical_title", "seniority", "soc_code", "soc_title"):
                val = parsed.get(key)
                if isinstance(val, str) and val.strip():
                    result[key] = val.strip()
            # Normalize SOC: must look like "NN-NNNN"
            soc = result.get("soc_code") or ""
            soc_match = re.search(r"\b(\d{2}-\d{4})\b", soc)
            result["soc_code"] = soc_match.group(1) if soc_match else None
    except Exception as e:
        if _is_fd_exhaustion(e):
            raise ResourceExhausted(
                "Process is out of file descriptors — restart the server to recover."
            ) from e
        logger.warning("classify_job_role: LLM call failed for title=%r (%s)", title, e)

    await db.set_salary_cache(cache_key, result)
    return result


def _fallback_canonical_title(title: str) -> str:
    """Strip seniority/level prefixes off a job title for use as a search query."""
    cleaned = re.sub(
        r"\b(senior|sr\.?|junior|jr\.?|staff|principal|lead|chief|head\s+of|vp|director|manager|intern|entry[-\s]?level)\b",
        "",
        title,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"[\(\[\{].*?[\)\]\}]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -,/|")
    return cleaned or title


def _fallback_seniority(title: str) -> str:
    t = title.lower()
    for kw, level in (
        ("intern", "intern"),
        ("entry", "entry"),
        ("junior", "junior"),
        (" jr", "junior"),
        ("staff", "staff"),
        ("principal", "principal"),
        ("senior", "senior"),
        (" sr", "senior"),
        ("director", "director"),
        ("manager", "manager"),
        ("lead", "senior"),
    ):
        if kw in t:
            return level
    return "mid"


# ---------------------------------------------------------------------------
# Adzuna histogram lookup (primary source)
# ---------------------------------------------------------------------------

ADZUNA_HISTOGRAM_URL = "https://api.adzuna.com/v1/api/jobs/{country}/histogram"


async def lookup_adzuna_histogram(
    *,
    what: str,
    where: str,
    country: str,
    app_id: str,
    app_key: str,
    cache_ttl_days: int = 30,
) -> Optional[dict]:
    """Fetch the Adzuna salary histogram for `what` in `where` and derive a range.

    Adzuna returns: {histogram: {bucket_start: count, ...}, mean: float, ...}
    We compute a count-weighted p25/p75 from the histogram buckets and use
    those as the estimated range. Caches the result for 30 days under
    "adzuna:{country}:{what}:{where}".
    """
    if not what:
        return None

    cache_key = f"adzuna:{country}:{what.lower()}:{(where or 'any').lower()}"
    cached = await db.get_salary_cache(cache_key, max_age_days=cache_ttl_days)
    if cached is not None:
        return cached

    params = {
        "app_id": app_id,
        "app_key": app_key,
        "what": what,
    }
    if where:
        params["where"] = where
    url = ADZUNA_HISTOGRAM_URL.format(country=country)

    try:
        client = await _get_cached_adzuna_client()
        resp = await client.get(url, params=params)
    except QuotaExceeded:
        raise
    except Exception as e:
        if _is_fd_exhaustion(e):
            raise ResourceExhausted(
                "Process is out of file descriptors — restart the server to recover."
            ) from e
        raise
    if resp.status_code in (429, 403):
        raise QuotaExceeded(
            f"adzuna {resp.status_code}: {resp.text[:200]}"
        )
    if resp.status_code >= 400:
        logger.warning("adzuna histogram %s -> %s %s", url, resp.status_code, resp.text[:200])
        return None
    data = resp.json()

    histogram = data.get("histogram") or {}
    if not histogram:
        return None

    p25, p50, p75 = _percentiles_from_histogram(histogram)
    if not p25 or not p75:
        return None

    sample_size = sum(int(v) for v in histogram.values() if isinstance(v, (int, float)))
    confidence = "low"
    if sample_size >= 200:
        confidence = "high"
    elif sample_size >= 50:
        confidence = "medium"

    payload = {
        "min": int(p25),
        "max": int(p75),
        "period": "annual",
        "source": "adzuna",
        "confidence": confidence,
        "metadata": {
            "p25": int(p25),
            "p50": int(p50) if p50 else None,
            "p75": int(p75),
            "sample_size": sample_size,
            "what": what,
            "where": where or None,
            "country": country,
        },
    }
    await db.set_salary_cache(cache_key, payload)
    return payload


def _percentiles_from_histogram(histogram: dict) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Compute count-weighted p25/p50/p75 from an Adzuna histogram.

    Adzuna histograms are dicts {bucket_start_salary_str: count}. Buckets are
    treated as their lower bound. For percentiles we walk the cumulative
    distribution and linearly interpolate inside the bucket containing the
    target rank.
    """
    try:
        buckets = sorted(
            ((float(k), int(v)) for k, v in histogram.items() if int(v) > 0),
            key=lambda kv: kv[0],
        )
    except (TypeError, ValueError):
        return (None, None, None)
    if not buckets:
        return (None, None, None)

    total = sum(c for _, c in buckets)
    if total == 0:
        return (None, None, None)

    def pct(p: float) -> float:
        target = total * p
        cum = 0
        for i, (lo, count) in enumerate(buckets):
            if cum + count >= target:
                next_lo = buckets[i + 1][0] if i + 1 < len(buckets) else lo * 1.1
                # Linear interpolation within the bucket
                frac = (target - cum) / count if count else 0
                return lo + frac * (next_lo - lo)
            cum += count
        return buckets[-1][0]

    return (pct(0.25), pct(0.50), pct(0.75))


# ---------------------------------------------------------------------------
# BLS OEWS lookup (secondary; optional)
# ---------------------------------------------------------------------------

BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

# BLS OEWS series: OEU{N|S|M}{areacode}000000{occupation_code_no_dashes}{datatype}
# datatype 04 = annual mean wage, 13 = 90th pctile, 12 = 75th, 11 = median,
# 10 = 25th, 09 = 10th. We pull 10/11/12.
_BLS_DATATYPES = {"p25": "10", "p50": "11", "p75": "12"}


def lookup_bls_oews(soc_code: str, msa_code: Optional[str], api_key: Optional[str]) -> Optional[dict]:
    """Synchronous BLS OEWS percentile lookup.

    soc_code like "15-1252"; msa_code is a 7-digit BLS MSA code or None for
    national. Returns the same shape as Adzuna or None on failure.
    """
    if not soc_code:
        return None

    occ = soc_code.replace("-", "").rjust(6, "0")
    if msa_code:
        prefix = f"OEUM{msa_code}000000"
        scope = f"msa:{msa_code}"
    else:
        prefix = "OEUN0000000000"  # national series prefix for OEWS
        scope = "national"

    series_ids = [prefix + occ + dt for dt in _BLS_DATATYPES.values()]

    body = {"seriesid": series_ids}
    if api_key:
        body["registrationkey"] = api_key

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(BLS_API_URL, json=body)
        if resp.status_code >= 400:
            logger.warning("BLS API %s returned %s", series_ids, resp.status_code)
            return None
        data = resp.json()
    except Exception:
        logger.exception("BLS API request failed")
        return None

    if data.get("status") != "REQUEST_SUCCEEDED":
        logger.warning("BLS API status=%s message=%s", data.get("status"), data.get("message"))
        return None

    pcts: dict[str, Optional[float]] = {"p25": None, "p50": None, "p75": None}
    for series in data.get("Results", {}).get("series", []) or []:
        sid = series.get("seriesID", "")
        for label, code in _BLS_DATATYPES.items():
            if sid.endswith(code):
                points = series.get("data") or []
                if points:
                    try:
                        pcts[label] = float(points[0].get("value"))
                    except (TypeError, ValueError):
                        pass
                break

    if not pcts["p25"] or not pcts["p75"]:
        return None

    return {
        "min": int(pcts["p25"]),
        "max": int(pcts["p75"]),
        "period": "annual",
        "source": "bls_oews",
        "confidence": "high" if msa_code else "medium",
        "metadata": {
            "soc_code": soc_code,
            "msa_code": msa_code,
            "scope": scope,
            "p25": int(pcts["p25"]) if pcts["p25"] else None,
            "p50": int(pcts["p50"]) if pcts["p50"] else None,
            "p75": int(pcts["p75"]) if pcts["p75"] else None,
        },
    }


# Minimal BLS MSA mapping — covers common metros plus the user's home market.
# Anything else falls through to national. Expand as needed.
_MSA_CODES = {
    "denver": "1974000",
    "boulder": "1474000",
    "colorado springs": "1782000",
    "new york": "3562000",
    "san francisco": "4194000",
    "san jose": "4194000",
    "los angeles": "3108000",
    "seattle": "4274000",
    "boston": "1471650",
    "chicago": "1697600",
    "austin": "1242000",
    "dallas": "1910000",
    "houston": "2642000",
    "atlanta": "1206200",
    "washington": "4790000",
    "philadelphia": "3798000",
    "phoenix": "3806000",
    "miami": "3310000",
    "minneapolis": "3346000",
    "san diego": "4174000",
    "portland": "3890000",
}


def location_to_msa(location: str) -> Optional[str]:
    """Map a free-text location string to a BLS MSA code, or None for national."""
    if not location:
        return None
    t = location.lower()
    for needle, code in _MSA_CODES.items():
        if needle in t:
            return code
    return None
