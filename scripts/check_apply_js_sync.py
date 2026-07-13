#!/usr/bin/env python3
"""
check_apply_js_sync.py — keep the injected Apply JS in sync across consumers.

snapshot.js / fill.js are injected by three different things:

  * the browser extension  — extension/src/common/{snapshot,fill}.js  (the source of truth)
  * the iOS Apply browser  — ios-standalone/App/Apply/JS/{snapshot,fill}.js (WKWebView)
  * the backend Playwright driver — backend/auto_apply/browser_controller.py::_SNAPSHOT_JS

They are hand-copied, with nothing checking that the copies still agree. As of
this writing the code is identical but the header comments have already drifted,
which is exactly how a real behavioural drift starts.

This compares the copies with comments and blank lines stripped, so the iOS file
can keep its own "this is a copy, keep in sync" header without tripping the check.

    check_apply_js_sync.py           # verify (exit 1 on drift)  -- used by CI
    check_apply_js_sync.py --fix     # re-copy from the extension, keeping headers
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "extension" / "src" / "common"
IOS_DIR = ROOT / "ios-standalone" / "App" / "Apply" / "JS"
FILES = ("snapshot.js", "fill.js")

_LINE_COMMENT = re.compile(r"^\s*//.*$")


def code_only(text: str) -> str:
    """Strip full-line // comments and blank lines — compare behaviour, not prose."""
    return "\n".join(
        line for line in text.splitlines()
        if line.strip() and not _LINE_COMMENT.match(line)
    )


def header_of(text: str) -> str:
    """The leading //-comment block a copy uses to explain that it is a copy."""
    lines, out = text.splitlines(), []
    for line in lines:
        if _LINE_COMMENT.match(line) or not line.strip():
            out.append(line)
        else:
            break
    return "\n".join(out).rstrip() + "\n\n" if out else ""


def main() -> int:
    fix = "--fix" in sys.argv
    drifted: list[str] = []

    for name in FILES:
        src, dst = SOURCE_DIR / name, IOS_DIR / name
        if not src.exists() or not dst.exists():
            print(f"::error:: missing {src if not src.exists() else dst}")
            return 1

        src_text, dst_text = src.read_text(), dst.read_text()
        if code_only(src_text) == code_only(dst_text):
            print(f"ok    {name}")
            continue

        drifted.append(name)
        if fix:
            dst.write_text(header_of(dst_text) + code_only(src_text) + "\n")
            print(f"fixed {name} — re-copied from {src.relative_to(ROOT)}")
        else:
            print(f"DRIFT {name}: {dst.relative_to(ROOT)} no longer matches "
                  f"{src.relative_to(ROOT)}")

    if drifted and not fix:
        print("\nThe extension is the source of truth. Run:\n"
              "    python3 scripts/check_apply_js_sync.py --fix")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
