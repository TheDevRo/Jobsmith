"""ONNX Runtime NLI scorer. Imported only when the Local AI model beta is on and the model is installed."""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

logger = logging.getLogger(__name__)

MAX_LENGTH = 512
BATCH_SIZE = 16
_lock = threading.Lock()
_scorer: "OnnxNLI | None" = None


class OnnxNLI:
    """[P(entailment), P(neutral), P(contradiction)] per (premise, hypothesis) pair."""

    def __init__(self, model_dir: Path):
        t0 = time.perf_counter()
        self.tok = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tok.enable_truncation(MAX_LENGTH, strategy="only_first")
        self.tok.enable_padding(pad_id=0, pad_token="[PAD]")
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(str(model_dir / "model.onnx"), opts, providers=_providers())
        self.inputs = {i.name for i in self.session.get_inputs()}
        self.lock = threading.Lock()  # one inference at a time; ORT already uses every core
        logger.info("NLI model loaded in %.1fs (%s)", time.perf_counter() - t0, self.session.get_providers()[0])

    def probs(self, pairs: list[tuple[str, str]]) -> list[list[float]]:
        out: list[list[float]] = [[]] * len(pairs)
        order = sorted(range(len(pairs)), key=lambda i: len(pairs[i][0]) + len(pairs[i][1]))  # less padding
        with self.lock:
            for s in range(0, len(order), BATCH_SIZE):
                idx = order[s:s + BATCH_SIZE]
                enc = self.tok.encode_batch([pairs[i] for i in idx])
                feed = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                        "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
                if "token_type_ids" in self.inputs:
                    feed["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
                logits = self.session.run(None, feed)[0].astype(np.float64)
                p = np.exp(logits - logits.max(-1, keepdims=True))
                p /= p.sum(-1, keepdims=True)
                for i, row in zip(idx, p.tolist(), strict=True):
                    out[i] = [round(v, 5) for v in row]  # labels: 0 entailment, 1 neutral, 2 contradiction
        return out

    def entail(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [p[0] for p in self.probs(pairs)]

    def count_tokens(self, premise: str, hypothesis: str) -> int:
        """Untruncated pair length, to know when a premise must be split."""
        with self.lock:
            self.tok.no_truncation()
            self.tok.no_padding()
            try:
                return len(self.tok.encode(premise, hypothesis).ids)
            finally:
                self.tok.enable_truncation(MAX_LENGTH, strategy="only_first")
                self.tok.enable_padding(pad_id=0, pad_token="[PAD]")


def _providers() -> list[str]:
    # ponytail: CPU only. CoreML was measured slower/unsupported for this int8 graph (the CoreML EP ran about a third of the graph and was 3.3x slower).
    return ["CPUExecutionProvider"]


def get(model_dir: Path) -> OnnxNLI:
    """Process-wide cached session (loading takes a few seconds)."""
    global _scorer
    with _lock:
        if _scorer is None:
            _scorer = OnnxNLI(model_dir)
        return _scorer


def unload() -> None:
    global _scorer
    with _lock:
        _scorer = None


if __name__ == "__main__":  # smoke: python -m backend.nli.runtime <model_dir>
    s = OnnxNLI(Path(sys.argv[1]))
    print(s.probs([("The candidate lives in Denver.", "The candidate lives in Colorado."),
                   ("The candidate lives in Denver.", "The candidate lives in Paris.")]))
