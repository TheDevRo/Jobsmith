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
from pathlib import Path


def project_root() -> Path:
    env = os.environ.get("JOBSMITH_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent
