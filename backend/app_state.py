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
import copy
import logging
import os
import time as _time
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

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
VALID_RESUME_STYLES = {"executive", "ledger", "banner", "compact", "swiss"}
# Retired style names still present in older configs — mapped on read.
LEGACY_RESUME_STYLES = {"standard": "ledger", "modern": "ledger", "minimal": "swiss"}
VALID_RESUME_ACCENTS = {"default", "navy", "burgundy", "forest", "plum", "charcoal"}
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


# load_config() is called on every request and on every background-task tick, and
# it used to re-parse config.yaml each time. Cache on mtime instead: stat() is
# cheap, and save_config() goes through os.replace(), which bumps mtime — so a
# hot-edit of the file (by us or by hand) is still picked up on the next call.
_config_cache: tuple[float, int, dict] | None = None  # (mtime, size, parsed)


def load_config() -> dict:
    global _config_cache
    try:
        st = CONFIG_PATH.stat()
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        stamp = None

    if _config_cache is not None and stamp is not None:
        cached_mtime, cached_size, cached_cfg = _config_cache
        if (cached_mtime, cached_size) == stamp:
            # Copy so a caller mutating the result can't poison the cache.
            return _apply_env_overrides(copy.deepcopy(cached_cfg))

    with open(CONFIG_PATH, "r") as f:
        raw = f.read()
    try:
        cfg = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        # A hand-edited, malformed config.yaml used to silently become {} — i.e.
        # the app would quietly run on defaults with the user's real settings
        # ignored. Prefer the last-known-good parse and make the failure loud.
        logger.exception("config.yaml is not valid YAML — keeping the last good config")
        if _config_cache is not None:
            return _apply_env_overrides(copy.deepcopy(_config_cache[2]))
        raise

    if stamp is not None:
        _config_cache = (stamp[0], stamp[1], copy.deepcopy(cfg))
    return _apply_env_overrides(cfg)


def save_config(cfg: dict) -> None:
    # Write-then-rename so a crash/kill mid-write can't truncate config.yaml
    # (a truncated file makes load_config() return {} → backend silently runs
    # on defaults). os.replace() is atomic on the same filesystem.
    #
    # Resolve symlinks first. In Docker, /app/config.yaml is a symlink into the
    # bind-mounted ./config directory; renaming onto the symlink path would
    # *replace the symlink* with a regular file in the container layer, so every
    # save would stop reaching the mounted file and the next boot's `ln -sf`
    # would wipe the user's settings. Renaming onto the resolved target writes
    # through the link instead, and keeping the temp file beside that target
    # keeps the rename atomic and on one filesystem.
    target = Path(os.path.realpath(CONFIG_PATH))
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        f.flush()
        os.fsync(f.fileno())
    # config.yaml holds the Workday/ATS passwords and every API key, so it must
    # not be world-readable. chmod the temp file *before* the rename, so there is
    # never a moment where a 0644 config.yaml exists on disk.
    try:
        os.chmod(tmp, 0o600)
    except OSError:  # best effort (e.g. a filesystem without POSIX modes)
        pass
    os.replace(tmp, target)


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


def task_running(key: str) -> bool:
    """True when a background worker under `key` is still in flight.

    Endpoints that kick off work used to overwrite running_tasks[key]
    unconditionally, so a double-POST spawned a second racing worker and orphaned
    the first handle (it could then never be cancelled). Callers check this and
    return 409 instead.
    """
    task = running_tasks.get(key)
    return bool(task and not task.done())


# Bundled-Chromium install progress, published by packaging/desktop_entry.py's
# background installer thread and read by /api/system/browser-status. Starts as
# "ready" because every non-desktop deployment (docker, source checkout) brings
# its own browser — only the desktop app has an install step to report on.
browser_install_status: dict = {"status": "ready", "error": None}

# Soft-stop flag: when True, a cancelled fetch falls through to the save phase
# with whatever partial results have been collected (vs. discarding them).
fetch_keep_partial: bool = False

# App ID currently being auto-applied (set by _bg_apply, cleared on exit).
# Used by pause/resume endpoints to update DB status without cancelling the task.
current_apply_app_id: Optional[str] = None

fetch_status: dict = {"active": False, "phase": "", "detail": "", "sources_done": 0, "sources_total": 0, "jobs_found": 0, "jobs_inserted": 0}
# Batch-scoring progress feed, read by GET /api/jobs/score-batch/status.
# status: idle | scoring | done | error | cancelled. `current` is the job being
# scored right now ("title · company"); done/total drive the progress bar.
score_status: dict = {"status": "idle", "done": 0, "total": 0, "current": "", "detail": "", "started_at": None, "finished_at": None}
detect_types_status: dict = {"active": False, "processed": 0, "easy_apply": 0, "quick_apply": 0, "external": 0, "unknown": 0, "detail": ""}
refetch_status: dict = {"active": False, "processed": 0, "total": 0, "updated": 0, "failed": 0, "detail": ""}
