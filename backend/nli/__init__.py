"""Local AI model (beta): one on-device NLI model behind one Settings switch.

`ai.nli_beta.enabled` (default off) turns on two things:
  * Apply Assist pass 4 answers leftover fields extractively (backend/auto_apply/extractive);
  * job-fit scoring falls back to the local model when the scoring LLM is unavailable.

Off means none of this runs: callers check `enabled()` (a dict lookup) first, and
onnxruntime / tokenizers are imported only by `get_scorer()` once the model is installed.
"""
from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class NLIScorer(Protocol):
    def probs(self, pairs: list[tuple[str, str]]) -> list[list[float]]:
        """[P(entailment), P(neutral), P(contradiction)] per (premise, hypothesis)."""

    def entail(self, pairs: list[tuple[str, str]]) -> list[float]: ...


def enabled(cfg: dict) -> bool:
    return bool(((cfg.get("ai") or {}).get("nli_beta") or {}).get("enabled"))


def get_scorer(cfg: dict) -> NLIScorer | None:
    """The loaded model, or None when the switch is off, the model isn't installed, or it won't load."""
    if not enabled(cfg):
        return None
    from . import model
    if not model.installed():
        return None
    try:
        from . import runtime
        return runtime.get(model.model_dir())
    except Exception:  # noqa: BLE001 — a broken model must never break the LLM path
        logger.exception("Local AI model failed to load; using the LLM instead")
        return None


def status(cfg: dict) -> dict:
    """GET /api/ai/nli/status payload. `installed` lets Settings offer Delete while the switch is off."""
    from . import model
    s = {"enabled": enabled(cfg), "installed": model.installed(), **model.status()}
    if not s["enabled"] and s["state"] != "downloading":
        s["state"] = "off"
    return s
