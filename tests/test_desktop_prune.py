"""prune_stale_chromium keeps only the live Playwright revision (full + headless shell)."""
import importlib.util
import sys
import types
from pathlib import Path

# packaging/ shadows the pip "packaging" library, so load the file by path.
_spec = importlib.util.spec_from_file_location(
    "desktop_entry", Path(__file__).parent.parent / "packaging" / "desktop_entry.py")
desktop_entry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(desktop_entry)


def test_prunes_old_chromium_and_headless_shell(tmp_path, monkeypatch):
    for name in ("chromium-1100", "chromium-1200", "chromium_headless_shell-1100",
                 "chromium_headless_shell-1200", "ffmpeg-1010"):
        (tmp_path / name).mkdir()
    exe = tmp_path / "chromium-1200" / "chrome-mac" / "Chromium"
    exe.parent.mkdir()
    exe.touch()

    class _P:
        chromium = types.SimpleNamespace(executable_path=str(exe))
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake = types.ModuleType("playwright.sync_api")
    fake.sync_playwright = lambda: _P()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)

    desktop_entry.prune_stale_chromium(tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "chromium-1200", "chromium_headless_shell-1200", "ffmpeg-1010"]
