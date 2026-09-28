"""Download, verify, locate and delete the Quick match model (embedding triage), like model.py for the NLI model.

Files land in <app data>/models/triage/<revision>/, each streamed + SHA-256 checked by model._download. Which
embedding model this is lives in the files (the ONNX export and triage-data.json), not in code: swapping models
means new release files and new pins below.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
from pathlib import Path

from ..paths import project_root
from .model import _download

logger = logging.getLogger(__name__)

# bge-small-en-v1.5 (BAAI, MIT): fp32 ONNX export with CLS pooling + L2 norm inside (input `ids`, mask = ids != 0),
# its tokenizer, and the triage weights / soft-skill + reference-people embeddings (synthetic people only).
REVISION = "bge-small-en-v1.5-triage-v1"
ONNX_FILE, TOKENIZER_FILE, DATA_FILE = "triage-bge-small-en-v1.5.onnx", "tokenizer.json", "triage-data.json"
FILES = {  # name (same under the download URL and on disk) -> (size, sha256)
    ONNX_FILE: (133055403, "4ea70f0c8cdbabfdab6e6f7b3767d4126e715b07b35e0409d7bb71df1be932b8"),
    TOKENIZER_FILE: (711396, "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"),
    DATA_FILE: (274937, "f757b61f69af1c178a712bfcdf4ae2a44c50cc238f0cc87f132176b9ce1c7468"),
}
SIZE_BYTES = sum(size for size, _ in FILES.values())
# A model-only pre-release, not an app release. JOBSMITH_TRIAGE_MODEL_URL overrides it (a mirror, or tests).
DEFAULT_BASE_URL = "https://github.com/TheDevRo/Jobsmith/releases/download/triage-model-v1"

_lock = threading.Lock()
_job: dict = {"thread": None, "done": 0, "error": None}


def base_url() -> str:
    return (os.environ.get("JOBSMITH_TRIAGE_MODEL_URL") or DEFAULT_BASE_URL).rstrip("/")


def model_dir() -> Path:
    return project_root() / "models" / "triage" / REVISION


def installed() -> bool:
    return all((model_dir() / name).is_file() for name in FILES)


def status() -> dict:
    """{state: not_installed|downloading|ready|error, progress 0-1, size_bytes, error}"""
    with _lock:
        running = _job["thread"] is not None and _job["thread"].is_alive()
        done, error = _job["done"], _job["error"]
    if running:
        return {"state": "downloading", "progress": round(done / SIZE_BYTES, 3), "size_bytes": SIZE_BYTES, "error": None}
    if installed():
        return {"state": "ready", "progress": 1.0, "size_bytes": SIZE_BYTES, "error": None}
    return {"state": "error" if error else "not_installed", "progress": 0.0, "size_bytes": SIZE_BYTES, "error": error}


def install() -> dict:
    """Start (or resume) the download in the background. No-op when running or installed."""
    with _lock:
        running = _job["thread"] is not None and _job["thread"].is_alive()
        if not running and not installed():
            _job.update(done=0, error=None, thread=threading.Thread(target=_download_all, name="triage-model-download",
                                                                    daemon=True))
            _job["thread"].start()
    return status()


def delete() -> dict:
    with _lock:
        if _job["thread"] is not None and _job["thread"].is_alive():
            raise RuntimeError("The model is still downloading")
        _job["error"] = None
    tr = sys.modules.get(__package__ + ".triage")
    if tr:
        tr.unload()
    shutil.rmtree(model_dir().parent, ignore_errors=True)
    return status()


def _add(n: int) -> None:
    with _lock:
        _job["done"] += n


def _download_all() -> None:
    d = model_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        for name, (size, sha) in FILES.items():
            if (d / name).is_file():
                _add(size)
                continue
            _download(f"{base_url()}/{name}", d / name, size, sha, add=_add)
        logger.info("Quick match model installed at %s", d)
    except Exception as exc:  # noqa: BLE001 — reported through status(), retried by install()
        logger.warning("Quick match model download failed: %s", exc)
        with _lock:
            _job["error"] = str(exc) or type(exc).__name__
