"""
routers/system.py — Health, notifications, LM Studio model management,
debug diagnostics, and generated-document downloads.
"""

import asyncio
import logging
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import app_state as state
from .. import database as db
from .. import ai_engine
from ..paths import reveal_in_file_manager

logger = logging.getLogger(__name__)

router = APIRouter()


# ---- Logs (Settings → Logs tab) ----

_TAIL_READ_CAP = 2_000_000  # trailing bytes to scan; plenty for 5000 lines


@router.get("/api/logs/tail")
async def tail_logs(request: Request, lines: int = Query(500, ge=10, le=5000)):
    """Return the last N lines of the backend log. Loopback only — the log
    can contain sensitive values (the extension token is logged at startup)."""
    if not state.is_loopback_request(request):
        raise HTTPException(403, "Only served to localhost")
    path = state.LOG_FILE
    if not path.exists():
        return {"lines": [], "path": str(path), "size": 0}
    size = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size - _TAIL_READ_CAP))
        text = f.read().decode("utf-8", errors="replace")
    return {"lines": text.splitlines()[-lines:], "path": str(path), "size": size}


@router.post("/api/logs/reveal")
async def reveal_log_file(request: Request):
    """Highlight the log file in the system file manager. Loopback only."""
    if not state.is_loopback_request(request):
        raise HTTPException(403, "Only served to localhost")
    if not state.LOG_FILE.exists():
        raise HTTPException(404, "No log file yet")
    return {"revealed": reveal_in_file_manager(state.LOG_FILE)}


@router.get("/api/notifications")
async def get_notifications(since_id: int = Query(0, ge=0)):
    """Return notification events newer than since_id."""
    events = [e for e in state.notification_queue if e["id"] > since_id]
    return {"notifications": events}


@router.get("/api/health")
async def health_check():
    cfg = state.load_config()
    ai_status = await ai_engine.test_connection(cfg)
    return {
        "status": "ok",
        "database": str(db.DB_PATH),
        "ai": ai_status,
    }


@router.get("/api/ai/models")
async def list_ai_models():
    """Return available models from the configured LM Studio instance."""
    cfg = state.load_config()
    status = await ai_engine.test_connection(cfg)
    if not status.get("connected"):
        raise HTTPException(503, detail=status.get("error", "LM Studio not reachable"))
    return {"models": status.get("models", [])}


class ReloadContextRequest(BaseModel):
    context_window: int


class LoadModelRequest(BaseModel):
    model: str
    context_window: int = 8192


@router.post("/api/ai/load-model")
async def load_model(body: LoadModelRequest):
    """Load a specific model in LM Studio with the given context window.

    Unlike /api/ai/reload-context this does NOT require a model to already be
    loaded — it is the first step when LM Studio is idle.

    Strategy (in order):
    1. Try LM Studio REST API  (POST /api/v1/models/load)
    2. Fall back to `lms load --context-length N -y <model>` CLI (local only).
    """
    import shutil

    if not body.model:
        raise HTTPException(400, detail="model is required")
    if body.context_window <= 0 or body.context_window % 1024 != 0:
        raise HTTPException(400, detail="context_window must be a positive multiple of 1024")

    cfg = state.load_config()
    base_url: str = cfg.get("ai", {}).get("base_url", "http://localhost:1234/v1")
    lms_root = base_url.rstrip("/")
    if lms_root.endswith("/v1"):
        lms_root = lms_root[:-3]

    from urllib.parse import urlparse as _urlparse
    _parsed = _urlparse(base_url)
    _lms_host = _parsed.hostname or ""
    _is_local = _lms_host in ("localhost", "127.0.0.1", "::1", "")

    # ── Step 1: LM Studio REST API ────────────────────────────────────────
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=180.0)) as client:
        try:
            r = await client.post(
                f"{lms_root}/api/v1/models/load",
                json={"model": body.model, "context_length": body.context_window},
            )
            rj = r.json() if r.content else {}
            if r.is_success and "error" not in rj:
                return {"loaded": True, "model": body.model, "method": "rest",
                        "context_window": body.context_window}
            rest_error = rj.get("error", f"HTTP {r.status_code}")
        except Exception as exc:
            rest_error = str(exc)

    # ── Step 2: lms CLI (local only) ──────────────────────────────────────
    lms_bin = shutil.which("lms") or str(Path.home() / ".lmstudio" / "bin" / "lms")
    if not _is_local:
        raise HTTPException(502, detail=(
            f"Could not load model via REST API: {rest_error}. "
            f"The lms CLI cannot control a remote LM Studio instance. "
            f"Open LM Studio on your homelab and load '{body.model}' manually."
        ))
    if not Path(lms_bin).exists():
        raise HTTPException(502, detail=(
            f"Could not load model via REST API: {rest_error}. "
            f"lms CLI not found at {lms_bin}. Load '{body.model}' in LM Studio manually."
        ))

    try:
        proc = await asyncio.create_subprocess_exec(
            lms_bin, "load", body.model,
            "--context-length", str(body.context_window),
            "-y",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        if proc.returncode == 0:
            return {"loaded": True, "model": body.model, "method": "lms-cli",
                    "context_window": body.context_window}
        err_text = (stderr or stdout or b"").decode().strip()[:400]
        raise HTTPException(502, detail=f"lms CLI failed: {err_text}")
    except asyncio.TimeoutError:
        raise HTTPException(504, detail="lms CLI timed out after 180s loading the model")


@router.post("/api/ai/reload-context")
async def reload_context_window(body: ReloadContextRequest):
    """Reload currently-loaded LM Studio model(s) with a new context window.

    Strategy (in order):
    1. Try LM Studio REST API  (POST /api/v0/models/load) — only works on newer builds.
    2. Fall back to `lms load --context-length N -y <model>` CLI — works wherever
       the lms CLI is installed and connected to the LM Studio instance.
    Also saves the preference to config.yaml.
    """
    import shutil

    cfg = state.load_config()
    base_url: str = cfg.get("ai", {}).get("base_url", "http://localhost:1234/v1")
    lms_root = base_url.rstrip("/")
    if lms_root.endswith("/v1"):
        lms_root = lms_root[:-3]

    context_window = body.context_window
    if context_window <= 0 or context_window % 1024 != 0:
        raise HTTPException(400, detail="context_window must be a positive multiple of 1024")

    # Save preference immediately regardless of reload outcome
    cfg["ai"]["context_window"] = context_window
    state.save_config(cfg)

    results: list[dict] = []
    errors:  list[dict] = []

    # ── Step 1: get loaded models via REST ────────────────────────────────
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, read=10.0)) as client:
        try:
            resp = await client.get(f"{lms_root}/api/v0/models")
            resp.raise_for_status()
            loaded_models = [m for m in resp.json().get("data", []) if m.get("state") == "loaded"]
        except Exception as e:
            raise HTTPException(502, detail=f"Could not reach LM Studio at {lms_root}: {e}")

    if not loaded_models:
        raise HTTPException(404, detail="No loaded models found in LM Studio — load a model first")

    # Determine whether LM Studio is local or remote.
    # The lms CLI only controls the LM Studio instance on *this* machine, so
    # it must never be used when base_url points to a remote host.
    from urllib.parse import urlparse as _urlparse
    _parsed_url = _urlparse(base_url)
    _lms_host = _parsed_url.hostname or ""
    _is_local = _lms_host in ("localhost", "127.0.0.1", "::1", "")

    # ── Step 2: try REST reload; fall back to lms CLI only for local instances ──
    lms_bin = shutil.which("lms") or str(Path.home() / ".lmstudio" / "bin" / "lms")
    lms_available = _is_local and Path(lms_bin).exists()

    # Group instances by base model id (strip ":N" suffix added by LM Studio
    # when multiple instances of the same model are loaded simultaneously).
    import re as _re
    base_model_instances: dict[str, list[str]] = {}
    for m in loaded_models:
        instance_id = m["id"]
        base_id = _re.sub(r":\d+$", "", instance_id)
        base_model_instances.setdefault(base_id, []).append(instance_id)

    for base_id, instance_ids in base_model_instances.items():
        # 2a. REST API — unload all instances, then reload at new context length
        rest_ok = False
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, read=120.0)) as client:
            try:
                # Unload every existing instance first (load always creates a new one)
                for iid in instance_ids:
                    await client.post(
                        f"{lms_root}/api/v1/models/unload",
                        json={"instance_id": iid},
                    )
                # Load fresh with the requested context length
                r = await client.post(
                    f"{lms_root}/api/v1/models/load",
                    json={"model": base_id, "context_length": context_window},
                    timeout=httpx.Timeout(10.0, read=120.0),
                )
                rj = r.json()
                if r.is_success and "error" not in rj:
                    rest_ok = True
                    results.append({"model": base_id, "method": "rest", "context_window": context_window})
                else:
                    errors.append({"model": base_id, "error": rj.get("error", str(rj))})
            except Exception as exc:
                errors.append({"model": base_id, "error": f"REST error: {exc}"})

        if rest_ok:
            continue

        # 2b. lms CLI fallback — local instances only
        if not _is_local:
            errors.append({
                "model": base_id,
                "error": (
                    f"LM Studio REST API reload failed. "
                    f"In LM Studio on {_lms_host}: click the loaded model → "
                    f"change Context Length to {context_window} → Reload."
                ),
            })
            continue

        if not lms_available:
            errors.append({"model": base_id, "error": f"REST API unsupported and lms CLI not found at {lms_bin}"})
            continue

        try:
            proc = await asyncio.create_subprocess_exec(
                lms_bin, "load", base_id,
                "--context-length", str(context_window),
                "-y",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            if proc.returncode == 0:
                results.append({"model": base_id, "method": "lms-cli", "context_window": context_window})
            else:
                err_text = (stderr or stdout or b"").decode().strip()[:400]
                errors.append({"model": base_id, "error": f"lms CLI failed: {err_text}"})
        except asyncio.TimeoutError:
            errors.append({"model": base_id, "error": "lms CLI timed out after 180s"})
        except Exception as e:
            errors.append({"model": base_id, "error": f"lms CLI error: {e}"})

    return {
        "context_window": context_window,
        "reloaded": results,
        "errors": errors,
        "api_supported": len(results) > 0,
    }


# ---------------------------------------------------------------------------
# Debug diagnostics endpoint (local use only)
# ---------------------------------------------------------------------------

@router.get("/api/debug/diag")
async def get_diag_files():
    """Read the last UI apply diagnostic files from /tmp and return their contents."""
    import json as _json
    keys = [
        "entry", "after_indeed", "after_linkedin", "before_ctrl", "before_launch",
        "launch_entry", "launch_persistent", "launch_new_ctx", "cookies_injected",
    ]
    result = {}
    for key in keys:
        path = Path(f"/tmp/UI_ljc_diag_{key}.json")
        if path.exists():
            try:
                result[key] = _json.loads(path.read_text())
            except Exception:
                result[key] = None
        else:
            result[key] = None
    return result


# ---------------------------------------------------------------------------
# File download endpoints
# ---------------------------------------------------------------------------

def _resolve_document(job_id: str, kind: str) -> tuple[Path, str, str]:
    """Resolve which generated file to serve for `kind` ('resume' or
    'cover_letter'), honoring the configured PDF/DOCX format and falling back
    to whichever extension actually exists.

    Returns (path, download_filename, media_type). Raises 404 if neither
    format is present.
    """
    cfg = state.load_config()
    fmt = cfg.get("application_honesty", {}).get("document_format", "docx")
    preferred = "pdf" if str(fmt).lower() == "pdf" else "docx"
    for ext in (preferred, "docx" if preferred == "pdf" else "pdf"):
        path = state.RESUMES_DIR / f"{job_id}_{kind}.{ext}"
        if path.exists():
            mime = "application/pdf" if ext == "pdf" else state.DOCX_MIME
            return path, f"{job_id}_{kind}.{ext}", mime
    raise HTTPException(404, f"{kind.replace('_', ' ').title()} not found")


@router.get("/api/resumes/{job_id}/resume")
async def download_resume(job_id: str):
    path, filename, mime = _resolve_document(job_id, "resume")
    return FileResponse(str(path), filename=filename, media_type=mime)


@router.get("/api/resumes/{job_id}/cover-letter")
async def download_cover_letter(job_id: str):
    path, filename, mime = _resolve_document(job_id, "cover_letter")
    return FileResponse(str(path), filename=filename, media_type=mime)
