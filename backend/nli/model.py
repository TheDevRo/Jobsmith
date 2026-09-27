"""Download, verify, locate and delete the local NLI model (Local AI model beta).

Files land in <app data>/models/nli/<revision>/. Each is streamed to a `.part`
file (resumed with an HTTP Range request after a network failure), SHA-256
checked, then atomically renamed, so the model directory only ever holds
verified files. The download runs in one daemon thread; `status()` reports it.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sys
import threading
from pathlib import Path

import httpx

from ..paths import project_root

logger = logging.getLogger(__name__)

# DeBERTa-v3-large-mnli-fever-anli-ling-wanli (MoritzLaurer), int8 weights: the public fp32 ONNX export
# (Xenova/..., revision a70e12f8) quantized with scripts/build_nli_model.py (8-bit MatMul
# blocks + int8 embedding; the public int8 exports fail the parity check). Pinned by SHA-256.
REVISION = "deberta-v3-large-wanli-w8-565e4c99"
# File name (the same under the download URL and on disk) -> (size, sha256)
FILES = {
    "model.onnx": (682207964, "565e4c99314132407a7beb19a6995065eb9053b77fce03b20acbcbb688bc33f5"),
    "tokenizer.json": (8648889, "7aa118770f066a74530d161c7d0b994d0629cc0ff3a0df213f184192773f960a"),
}
SIZE_BYTES = sum(size for size, _ in FILES.values())
# Where the two files are hosted: <base>/model.onnx and <base>/tokenizer.json (a model-only GitHub release,
# not an app release). JOBSMITH_NLI_MODEL_URL overrides it (a mirror, or tests).
DEFAULT_BASE_URL = "https://github.com/TheDevRo/Jobsmith/releases/download/nli-model-v1"
NOT_HOSTED = "The Local AI model is not available for download yet (no download location is set)"

_lock = threading.Lock()
_job: dict = {"thread": None, "done": 0, "error": None}


def base_url() -> str:
    return (os.environ.get("JOBSMITH_NLI_MODEL_URL") or DEFAULT_BASE_URL).rstrip("/")


def model_dir() -> Path:
    return project_root() / "models" / "nli" / REVISION


def installed() -> bool:
    d = model_dir()
    return all((d / name).is_file() for name in FILES)


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
            _job.update(done=0, error=None, thread=threading.Thread(target=_download_all, name="nli-model-download",
                                                                    daemon=True))
            _job["thread"].start()
    return status()


def delete() -> dict:
    """Remove every downloaded revision (and partial files). Refused while a download runs."""
    with _lock:
        if _job["thread"] is not None and _job["thread"].is_alive():
            raise RuntimeError("The model is still downloading")
        _job["error"] = None
    rt = sys.modules.get(__package__ + ".runtime")  # never import onnxruntime just to delete files
    if rt:
        rt.unload()
    shutil.rmtree(model_dir().parent, ignore_errors=True)
    return status()


def _download_all() -> None:
    d = model_dir()
    try:
        if not base_url():
            raise RuntimeError(NOT_HOSTED)
        d.mkdir(parents=True, exist_ok=True)
        for name, (size, sha) in FILES.items():
            if (d / name).is_file():
                _add(size)
                continue
            _download(f"{base_url()}/{name}", d / name, size, sha)
        logger.info("NLI model installed at %s", d)
    except Exception as exc:  # noqa: BLE001 — reported through status(), retried by install()
        logger.warning("NLI model download failed: %s", exc)
        with _lock:
            _job["error"] = str(exc) or type(exc).__name__


def _add(n: int) -> None:
    with _lock:
        _job["done"] += n


def _download(url: str, dest: Path, size: int, sha: str) -> None:
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have > size:
        part.unlink()
        have = 0
    h = hashlib.sha256()
    if have:
        with part.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    _add(have)
    if have < size:
        headers = {"Range": f"bytes={have}-"} if have else {}
        with httpx.stream("GET", url, headers=headers, follow_redirects=True,
                          timeout=httpx.Timeout(30.0, read=60.0)) as r:
            r.raise_for_status()
            if have and r.status_code != 206:  # server ignored the Range header: start over
                _add(-have)
                have, h = 0, hashlib.sha256()
            with part.open("ab" if have else "wb") as f:
                for chunk in r.iter_bytes():  # as received, so a dropped connection keeps what arrived
                    f.write(chunk)
                    h.update(chunk)
                    _add(len(chunk))
    if h.hexdigest() != sha:
        part.unlink(missing_ok=True)
        raise ValueError(f"{dest.name}: checksum mismatch (download corrupted or the file changed upstream)")
    os.replace(part, dest)
