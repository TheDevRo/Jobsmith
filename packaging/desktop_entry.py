"""
desktop_entry.py — Entry point for the PyInstaller-bundled desktop backend.

The Tauri shell spawns this as a sidecar. It:
  1. Points all user state (config, data/, resumes/, sessions/) at an
     app-data directory via JOBSMITH_HOME (see backend/paths.py).
  2. Installs Playwright's Chromium into that directory *in the background* —
     browsers are too big to ship in the installer, and a 150 MB download must
     not hold the dashboard hostage. Progress/failure lands in
     backend.app_state.browser_install_status, which the API exposes at
     GET /api/system/browser-status (and retries via POST .../browser-install).
  3. Boots uvicorn on port 8888 serving the bundled frontend, bound to
     127.0.0.1 unless server.host in config.yaml (or JOBSMITH_HOST) opts
     into LAN exposure.

Run outside a bundle it works too (handy for testing):
    venv/bin/python packaging/desktop_entry.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


def watch_parent(parent_pid: int, shell_pid: int = 0) -> None:
    """Exit when the Tauri shell dies.

    Two ways the shell can vanish:

    * Clean quit — Tauri kills the sidecar. PyInstaller one-file runs as a
      bootloader that forks the real server as a child, so that kill hits the
      bootloader and the forked child can outlive it, orphaning uvicorn. This
      watchdog runs *inside* that child: once our parent is gone (reparented to
      init, or the recorded PID no longer exists), take the whole process down.
    * Force-quit / crash — RunEvent::Exit never fires, so nobody kills the
      sidecar at all and uvicorn keeps holding port 8888 forever. The shell
      passes its own PID in JOBSMITH_SHELL_PID; poll it directly.
    """
    while True:
        if parent_pid > 1 and os.getppid() != parent_pid:
            os._exit(0)
        if shell_pid > 1:
            try:
                os.kill(shell_pid, 0)  # signal 0: existence check only
            except ProcessLookupError:
                print("[desktop] Tauri shell is gone — shutting the backend down.", flush=True)
                os._exit(0)
            except PermissionError:
                pass  # alive, just not ours to signal
        time.sleep(2)


def app_home() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Jobsmith"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "Jobsmith"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "Jobsmith"


def bind_host(home: Path) -> str:
    """Resolve the interface uvicorn binds to.

    Precedence: JOBSMITH_HOST env, then server.host in the app-home
    config.yaml (set from Settings → Integrations → Network), else loopback.

    The Tauri webview always connects via 127.0.0.1, so a specific
    non-loopback interface IP would leave the app window unable to reach its
    own backend — any non-loopback value binds 0.0.0.0 instead, which covers
    the requested interface and keeps loopback working.
    """
    host = os.environ.get("JOBSMITH_HOST", "").strip()
    if not host:
        try:
            import yaml
            with open(home / "config.yaml") as f:
                cfg = yaml.safe_load(f) or {}
            host = str((cfg.get("server") or {}).get("host") or "").strip()
        except Exception:
            host = ""  # missing/corrupt config must not block launch
    if not host or host in ("127.0.0.1", "localhost", "::1"):
        return "127.0.0.1"
    if host != "0.0.0.0":
        print(f"[desktop] server.host={host}: binding 0.0.0.0 so the app window "
              "(which connects via 127.0.0.1) can still reach the backend.", flush=True)
    return "0.0.0.0"


def browsers_path() -> Path:
    """Where Playwright browsers live for this install."""
    return Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or app_home() / "browsers")


def run_playwright_install(browsers_dir: Path) -> None:
    """Install Playwright's Chromium into the app-data dir. Raises on failure.

    No "is it already there?" glob guard: the old one (`any(glob("chromium*"))`)
    matched a *stale* revision after a Playwright bump — so the install was
    skipped forever and Playwright then failed at runtime looking for the new
    revision — and it also matched a half-finished download. The driver's own
    `install chromium` is idempotent and returns in ~1s when the pinned revision
    is present, so just always run it and let it be the source of truth.
    """
    browsers_dir.mkdir(parents=True, exist_ok=True)
    # Frozen bundles have no `playwright` CLI on PATH; drive the bundled
    # node driver directly, the same way `playwright install` does.
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    env = get_driver_env()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir)
    result = subprocess.run(
        [*compute_driver_executable(), "install", "chromium"],
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"playwright install chromium exited {result.returncode} "
            f"(browsers dir: {browsers_dir})"
        )


def prune_stale_chromium(browsers_dir: Path) -> None:
    """Delete chromium revisions the current Playwright no longer uses.

    Without this the folder grows ~150 MB on every Playwright bump. Best-effort:
    if we can't confidently identify the live revision, delete nothing.
    """
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            live = Path(p.chromium.executable_path).resolve()
    except Exception as exc:  # noqa: BLE001 — pruning must never break startup
        print(f"[desktop] Skipping stale-browser prune: {exc}", flush=True)
        return

    for entry in browsers_dir.glob("chromium-*"):
        if not entry.is_dir() or entry.resolve() in live.parents:
            continue
        print(f"[desktop] Removing stale browser revision {entry.name}", flush=True)
        shutil.rmtree(entry, ignore_errors=True)


def ensure_chromium(browsers_dir: Path | None = None, prune: bool = True) -> None:
    """Install Chromium, publishing progress into state.browser_install_status.

    Never raises: this runs on a daemon thread at startup (REL-04) and again on
    POST /api/system/browser-install, which both want the failure *reported*
    (so the UI can offer Retry), not thrown into a traceback. Callable with no
    args so the retry endpoint doesn't need to know where the browsers live.
    """
    from backend import app_state as state

    browsers_dir = browsers_dir or browsers_path()
    state.browser_install_status = {"status": "installing", "error": None}
    try:
        run_playwright_install(browsers_dir)
    except Exception as exc:  # noqa: BLE001 — surfaced to the UI, never fatal
        print(f"[desktop] WARNING: Chromium install failed: {exc} — auto-apply is "
              "unavailable until it succeeds (retry from Settings).", flush=True)
        state.browser_install_status = {"status": "failed", "error": str(exc)}
        return
    if prune:
        prune_stale_chromium(browsers_dir)
    state.browser_install_status = {"status": "ready", "error": None}
    print("[desktop] Chromium ready.", flush=True)


def main() -> None:
    home = app_home()
    home.mkdir(parents=True, exist_ok=True)

    os.environ["JOBSMITH_HOME"] = str(home)
    # An externally-set PLAYWRIGHT_BROWSERS_PATH wins (e.g. sharing the dev cache).
    browsers_dir = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or home / "browsers")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir)
    # Relative paths (e.g. the data/ JSONL log) resolve under the app home.
    os.chdir(home)

    parent_pid = os.getppid()
    try:
        shell_pid = int(os.environ.get("JOBSMITH_SHELL_PID") or 0)
    except ValueError:
        shell_pid = 0
    if parent_pid > 1 or shell_pid > 1:
        threading.Thread(
            target=watch_parent, args=(parent_pid, shell_pid), daemon=True
        ).start()

    # Chromium is a ~150 MB download on first run. Do it on a daemon thread so
    # uvicorn (and the dashboard) come up immediately; the UI polls
    # /api/system/browser-status for progress and offers a retry on failure.
    # Import app_state now (env vars are set, so its paths bake correctly) and
    # hand the API a retry hook.
    from backend import app_state as state

    # Only prune revisions inside our own app-home browsers dir — a shared
    # PLAYWRIGHT_BROWSERS_PATH (dev cache) may hold browsers other apps need.
    prune = browsers_dir == home / "browsers"
    state.browser_install_runner = lambda: ensure_chromium(browsers_dir, prune)
    threading.Thread(
        target=ensure_chromium, args=(browsers_dir, prune), daemon=True
    ).start()

    import uvicorn
    from backend.main import app  # import AFTER env vars: module-level paths bake at import

    # log_config=None: skip uvicorn's dictConfig so its loggers propagate to
    # the root logger and land in the rotating file (Settings → Logs).
    # access_log=False: the extension/notification polling would flood it.
    uvicorn.run(
        app,
        host=bind_host(home),
        port=int(os.environ.get("JOBSMITH_PORT", "8888")),
        log_level="info",
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
