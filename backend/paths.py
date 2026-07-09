"""
paths.py — Where user state (config, data/, resumes/, sessions/…) lives.

Default is the repo root, same as always. The desktop (Tauri/PyInstaller)
build sets JOBSMITH_HOME to an app-data directory — inside a frozen bundle,
__file__-relative paths point into the read-only (and on updates, replaced)
app bundle, which must never hold user state.

Code assets shipped with the app (frontend/, config.example.yaml) stay
__file__-relative on purpose; only user state keys off project_root().
"""

import os
import subprocess
import sys
from pathlib import Path


def project_root() -> Path:
    env = os.environ.get("JOBSMITH_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def pick_folder(prompt: str = "Select a folder") -> "str | None":
    """Open the OS-native folder picker and return the chosen absolute path,
    or None if the user cancelled (or the platform has no picker wired).

    Blocking — the dialog is modal — so callers must run this off the event
    loop (a plain `def` FastAPI handler runs in the threadpool, which is fine).
    """
    safe_prompt = prompt.replace('"', "'")
    try:
        if sys.platform == "darwin":
            # `choose folder` returns an alias; POSIX path stringifies it.
            script = f'POSIX path of (choose folder with prompt "{safe_prompt}")'
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=600,
            )
            if proc.returncode != 0:
                return None  # cancelled (osascript exits non-zero) or errored
            path = proc.stdout.strip()
            return path or None
        # No native picker on other platforms; caller keeps the text field.
        return None
    except (OSError, subprocess.SubprocessError):
        return None


def reveal_in_file_manager(path: Path) -> bool:
    """Best-effort: highlight a file/folder in Finder/Explorer/etc."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", f"/select,{path}"])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
        return True
    except OSError:
        return False
