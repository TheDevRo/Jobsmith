"""Generate extension icons (16/48/128 PNG) matching the Jobsmith favicon.

Mirrors frontend/favicon.svg: dark rounded square with an indigo clock face.
Run from anywhere; writes into extension/src/icons/.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

# Colors from frontend/favicon.svg
BG = (30, 30, 46, 255)         # #1e1e2e
RING = (99, 102, 241, 255)     # #6366f1
HANDS = (165, 180, 252, 255)   # #a5b4fc
DOT = RING

# SVG viewBox is 32x32. All geometry derived from that.
VB = 32.0
OUT_DIR = Path(__file__).resolve().parent.parent / "src" / "icons"


def _rounded_mask(size: int, radius: float) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=radius, fill=255
    )
    return mask


def make_icon(size: int) -> Image.Image:
    # Supersample for crisp strokes, then downscale.
    scale = 4
    s = size * scale
    k = s / VB  # px per SVG unit

    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded background (rx=8 in 32-unit space)
    radius = 8 * k
    draw.rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=BG)

    cx = cy = 16 * k
    r = 9 * k
    stroke = 2.2 * k

    # Outer ring
    draw.ellipse(
        (cx - r, cy - r, cx + r, cy + r),
        outline=RING,
        width=max(1, round(stroke)),
    )

    # Clock hands: M16 9 v7 (down to center) then to (20.5, 18.5)
    hand_w = max(1, round(stroke))
    p_top = (16 * k, 9 * k)
    p_mid = (16 * k, 16 * k)
    p_end = (20.5 * k, 18.5 * k)
    draw.line([p_top, p_mid], fill=HANDS, width=hand_w)
    draw.line([p_mid, p_end], fill=HANDS, width=hand_w)

    # Round caps for the hands (Pillow line endcaps are square)
    cap_r = hand_w / 2
    for px, py in (p_top, p_mid, p_end):
        draw.ellipse(
            (px - cap_r, py - cap_r, px + cap_r, py + cap_r),
            fill=HANDS,
        )

    # Center dot (r=1.5)
    dot_r = 1.5 * k
    draw.ellipse(
        (cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r),
        fill=DOT,
    )

    # Downsample
    img = img.resize((size, size), Image.LANCZOS)

    # Re-apply rounded-corner mask post-resize to keep the rim crisp.
    mask = _rounded_mask(size, max(2, round(size * 8 / VB)))
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in (16, 48, 128):
        out = OUT_DIR / f"icon-{size}.png"
        make_icon(size).save(out, format="PNG")
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
