"""Generate extension icons (16/48/128 PNG) from the Jobsmith app icon.

Derives every size from packaging/app-icon.png (the release artwork used
for the desktop icons) with rounded corners, so all branding stays in sync.
Run from anywhere; writes into extension/src/icons/.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE = REPO_ROOT / "packaging" / "app-icon.png"
OUT_DIR = Path(__file__).resolve().parent.parent / "src" / "icons"

# Corner radius as a fraction of icon size (matches the macOS squircle).
RADIUS = 0.225


def make_icon(source: Image.Image, size: int) -> Image.Image:
    img = source.convert("RGBA").resize((size, size), Image.LANCZOS)
    # Supersampled mask for smooth corners at tiny sizes.
    ss = size * 4
    mask = Image.new("L", (ss, ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, ss - 1, ss - 1), radius=round(ss * RADIUS), fill=255
    )
    img.putalpha(mask.resize((size, size), Image.LANCZOS))
    return img


def main() -> None:
    source = Image.open(SOURCE)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in (16, 48, 128):
        out = OUT_DIR / f"icon-{size}.png"
        make_icon(source, size).save(out, format="PNG")
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
