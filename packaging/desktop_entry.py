"""
desktop_entry.py — Entry point for the PyInstaller-bundled desktop backend.

The Tauri shell spawns this as a sidecar. It:
  1. Points all user state (config, data/, resumes/, sessions/) at an
     app-data directory via JOBSMITH_HOME (see backend/paths.py).
  2. Installs Playwright's Chromium into that directory on first run —
     browsers are too big to ship in the installer.
  3. Boots uvicorn on 127.0.0.1:8888 serving the bundled frontend.

Run outside a bundle it works too (handy for testing):
    venv/bin/python packaging/desktop_entry.py
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path


def watch_parent(parent_pid: int) -> None:
    """Exit when the Tauri shell dies.

    PyInstaller one-file runs as a bootloader that forks the real server as a
    child. When Tauri kills the sidecar PID it hits the bootloader, and the
    forked child can outlive it — orphaning the uvicorn server. This watchdog
    runs *inside* that child: once our parent is gone (reparented to init, or
    the recorded PID no longer exists), take the whole process down.
    """
    while True:
        if os.getppid() != parent_pid or parent_pid <= 1:
            os._exit(0)
        time.sleep(2)


def app_home() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Jobsmith"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "Jobsmith"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "Jobsmith"


def ensure_chromium(browsers_dir: Path) -> None:
    """First-run download of Playwright's Chromium into the app-data dir."""
    if browsers_dir.exists() and any(browsers_dir.glob("chromium*")):
        return
    print(f"[desktop] Installing Chromium into {browsers_dir} (first run)…", flush=True)
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
        print("[desktop] WARNING: Chromium install failed — auto-apply will not work "
              "until it succeeds (it retries on next launch).", flush=True)


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
    if parent_pid > 1:
        threading.Thread(target=watch_parent, args=(parent_pid,), daemon=True).start()

    ensure_chromium(browsers_dir)

    import uvicorn
    from backend.main import app  # import AFTER env vars: module-level paths bake at import

    # log_config=None: skip uvicorn's dictConfig so its loggers propagate to
    # the root logger and land in the rotating file (Settings → Logs).
    # access_log=False: the extension/notification polling would flood it.
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("JOBSMITH_PORT", "8888")),
        log_level="info",
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
