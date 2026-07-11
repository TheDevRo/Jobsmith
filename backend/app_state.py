"""
app_state.py — Shared paths, config access, and mutable runtime state.

Everything here is process-wide singletons shared by the routers in
backend/routers/ and the workers in backend/background_tasks.py.

Names that get *rebound* at runtime (not just mutated in place) — the status
dicts, flags, and counters — must be accessed as attributes of this module
(``state.fetch_status = {...}``) so every reader sees the new object. The
containers that are only mutated in place (``running_tasks``, the
notification queue, the cancel events) can be imported directly.
"""

import asyncio
import collections
import os
import time as _time
from pathlib import Path
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
# PROJECT_ROOT holds user state and honors JOBSMITH_HOME (desktop builds);
# _CODE_ROOT holds assets shipped with the app and must stay __file__-based.
from .paths import project_root

PROJECT_ROOT = project_root()
_CODE_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = _CODE_ROOT / "config.example.yaml"
FRONTEND_DIR = _CODE_ROOT / "frontend"
EXT_DIST_DIR = _CODE_ROOT / "extension" / "dist"
RESUMES_DIR = PROJECT_ROOT / "resumes"
SCREENSHOTS_DIR = PROJECT_ROOT / "data" / "screenshots"
LOGS_DIR = PROJECT_ROOT / "data" / "logs"
LOG_FILE = LOGS_DIR / "jobsmith.log"

JSONL_LOG_PATH = PROJECT_ROOT / "data" / "auto_apply_log.jsonl"
APPLY_LOG_V2_FIELDS = (
    "ts", "level", "message", "field_id", "value", "source",
    "confidence", "action", "provider", "tier", "status",
)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

VALID_HONESTY_LEVELS = {"honest", "tailored", "embellished", "fabricated"}
VALID_RESUME_STYLES = {"standard", "minimal", "modern"}
VALID_DOC_FORMATS = {"docx", "pdf"}
VALID_AI_EDIT_TIERS = {"fast", "strong"}


# Env vars that override config.yaml keys, mainly so the Docker image can be
# pointed at LM Studio/FlareSolverr without editing the mounted config.
# Maps env var → (section, key) in the config dict.
_ENV_OVERRIDES = {
    "JOBSMITH_AI_BASE_URL": ("ai", "base_url"),
    "JOBSMITH_AI_API_KEY": ("ai", "api_key"),
    "JOBSMITH_FLARESOLVERR_URL": ("flaresolverr", "url"),
    "JOBSMITH_HOST": ("server", "host"),
}


def _apply_env_overrides(cfg: dict) -> dict:
    # Quirk, accepted: the settings UI round-trips load_config() → save_config(),
    # which can bake an env-overridden value into config.yaml. Harmless — the
    # baked value equals the operative one, and env still wins on next load.
    for env_var, (section, key) in _ENV_OVERRIDES.items():
        value = os.environ.get(env_var)
        if value:
            if not isinstance(cfg.get(section), dict):
                cfg[section] = {}  # covers missing section and empty "section:" (None)
            cfg[section][key] = value
    return cfg


def load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return _apply_env_overrides(yaml.safe_load(f) or {})


def save_config(cfg: dict) -> None:
    # Write-then-rename so a crash/kill mid-write can't truncate config.yaml
    # (a truncated file makes load_config() return {} → backend silently runs
    # on defaults). os.replace() is atomic on the same filesystem.
    tmp = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    with open(tmp, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_PATH)


def is_loopback_request(request) -> bool:
    """True when the HTTP request originates from this machine."""
    client = getattr(request, "client", None)
    host = (client.host if client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


# ---------------------------------------------------------------------------
# Notification event queue
# ---------------------------------------------------------------------------
notification_queue: collections.deque = collections.deque(maxlen=100)
notification_counter = 0


def push_notification(ntype: str, title: str, message: str, status: str = "info"):
    """Push a notification event for the frontend to pick up."""
    global notification_counter
    notification_counter += 1
    notification_queue.append({
        "id": notification_counter,
        "type": ntype,
        "title": title,
        "message": message,
        "status": status,  # "success", "error", "info"
        "timestamp": _time.time(),
    })


# ---------------------------------------------------------------------------
# Background task coordination
# ---------------------------------------------------------------------------

# Cancellation events — set() to signal stop, clear() to reset
cancel_fetch = asyncio.Event()
cancel_score = asyncio.Event()
cancel_tailor = asyncio.Event()
cancel_apply = asyncio.Event()
cancel_detect_types = asyncio.Event()
cancel_estimate_salaries = asyncio.Event()
cancel_refetch = asyncio.Event()

# Track running asyncio.Tasks so we can cancel them
running_tasks: dict[str, asyncio.Task] = {}

# Soft-stop flag: when True, a cancelled fetch falls through to the save phase
# with whatever partial results have been collected (vs. discarding them).
fetch_keep_partial: bool = False

# App ID currently being auto-applied (set by _bg_apply, cleared on exit).
# Used by pause/resume endpoints to update DB status without cancelling the task.
current_apply_app_id: Optional[str] = None

fetch_status: dict = {"active": False, "phase": "", "detail": "", "sources_done": 0, "sources_total": 0, "jobs_found": 0, "jobs_inserted": 0}
detect_types_status: dict = {"active": False, "processed": 0, "easy_apply": 0, "quick_apply": 0, "external": 0, "unknown": 0, "detail": ""}
refetch_status: dict = {"active": False, "processed": 0, "total": 0, "updated": 0, "failed": 0, "detail": ""}
