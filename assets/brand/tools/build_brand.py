# /// script
# requires-python = ">=3.12"
# dependencies = ["fonttools==4.66.1"]
# ///
"""Build every tracedresearch brand asset from source, reproducibly.

    uv run assets/brand/tools/build_brand.py

Requires `resvg` on PATH for the PNG exports (`cargo install resvg --locked --version 0.48.1`).

The wordmark is set in JetBrains Mono ExtraBold and converted to outlines, so
the logos render identically without the font installed. The font is fetched
from the pinned upstream release and verified by SHA-256 (it is not vendored).
JetBrains Mono is licensed under the SIL Open Font License 1.1, which permits
using its outlines in artwork such as a logo.

The mark is a lens over a step trace: three committed steps joined by a path,
seen through a magnifier. It stands for "every step of the research run is
recorded and can be inspected".
"""

from __future__ import annotations

import hashlib
import io
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

OUT = pathlib.Path(__file__).resolve().parents[1]
FONT_URL = "https://github.com/JetBrains/JetBrainsMono/releases/download/v2.304/JetBrainsMono-2.304.zip"
FONT_SHA256 = "6f6376c6ed2960ea8a963cd7387ec9d76e3f629125bc33d1fdcd7eb7012f7bbf"

# Palette. One accent (teal), two tones so it passes contrast on both GitHub themes.
INK = "#0A1418"  # mark on light backgrounds, favicon tile, social background
MIST = "#E8F1F2"  # mark on dark backgrounds
TEAL_ON_LIGHT = "#0B7A75"
TEAL_ON_DARK = "#2DD4BF"
MUTED_ON_DARK = "#93A8AC"
GITHUB_LIGHT_BG = "#FFFFFF"
GITHUB_DARK_BG = "#0D1117"


# ----------------------------------------------------------------- contrast


def _lum(hex_color: str) -> float:
    rgb = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# ----------------------------------------------------------------- fonts


def load_fonts() -> dict[str, TTFont]:
    cache = OUT / "tools" / ".cache" / "JetBrainsMono-2.304.zip"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(FONT_URL, timeout=60) as r:
            cache.write_bytes(r.read())
    data = cache.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != FONT_SHA256:
        cache.unlink()
        sys.exit(f"font archive checksum mismatch: {digest}")
    z = zipfile.ZipFile(io.BytesIO(data))
    return {w: TTFont(io.BytesIO(z.read(f"fonts/ttf/JetBrainsMono-{w}.ttf"))) for w in ("ExtraBold", "Medium")}


def _ntos(n: float) -> str:
    s = f"{n:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def text_path(
    font: TTFont, text: str, size: float, x: float, baseline: float, tracking: float = 0.0
) -> tuple[str, float]:
    """Outline `text` as an SVG path. Returns (d, advance width)."""
    glyphs, cmap = font.getGlyphSet(), font.getBestCmap()
    scale = size / font["head"].unitsPerEm
    pen = SVGPathPen(glyphs, ntos=_ntos)
    cursor = x
    for ch in text:
        name = cmap[ord(ch)]
        glyphs[name].draw(TransformPen(pen, (scale, 0, 0, -scale, cursor, baseline)))
        cursor += font["hmtx"][name][0] * scale + tracking
    return pen.getCommands(), cursor - x - tracking


def x_height(font: TTFont, size: float) -> float:
    return font["OS/2"].sxHeight * size / font["head"].unitsPerEm


# ----------------------------------------------------------------- the mark


def mark(fg: str, accent: str, *, x: float = 0, y: float = 0, size: float = 64) -> str:
    """Lens over a step trace, on a 64-unit grid.

    Ring + handle in `fg`; inside the lens a step trace in `accent`: three
    committed steps (dots) and the path continuing to the next one."""
    s = size / 64
    return (
        f'<g transform="translate({_ntos(x)} {_ntos(y)}) scale({_ntos(s)})" fill="none" '
        'stroke-linecap="round" stroke-linejoin="round">'
        f'<circle cx="27" cy="27" r="18.5" stroke="{fg}" stroke-width="5.5"/>'
        f'<path stroke="{fg}" stroke-width="7.5" d="M40.5 40.5L54 54"/>'
        f'<path stroke="{accent}" stroke-width="3.6" d="M17 33.5L24.5 22.5L31 30.5L36.5 21"/>'
        f'<circle cx="17" cy="33.5" r="3.4" fill="{accent}" stroke="none"/>'
        f'<circle cx="24.5" cy="22.5" r="3.4" fill="{accent}" stroke="none"/>'
        f'<circle cx="31" cy="30.5" r="3.4" fill="{accent}" stroke="none"/>'
        "</g>"
    )


def favicon_mark() -> str:
    """32px variant: own tile, heavier strokes, two-segment trace."""
    return (
        f'<rect width="32" height="32" rx="7" fill="{INK}"/>'
        '<g fill="none" stroke-linecap="round" stroke-linejoin="round">'
        f'<circle cx="14" cy="14" r="8.2" stroke="{MIST}" stroke-width="2.8"/>'
        f'<path stroke="{MIST}" stroke-width="3.6" d="M20.2 20.2L26 26"/>'
        f'<path stroke="{TEAL_ON_DARK}" stroke-width="2.2" d="M9.6 16.8L13 11.6L16.4 15.6L18.4 12"/>'
        "</g>"
    )


def favicon16_mark() -> str:
    """Hand-hinted 16px variant, every edge on the pixel grid (crispEdges):
    an octagonal 1px lens, a 2px diagonal handle and a teal zig-zag step trace."""
    handle = "".join(f'<rect x="{i}" y="{i}" width="2" height="2" fill="{MIST}"/>' for i in (10, 11, 12))
    return (
        f'<rect width="16" height="16" rx="3" fill="{INK}"/>'
        '<g shape-rendering="crispEdges">'
        f'<path fill="none" stroke="{MIST}" stroke-width="1" stroke-linecap="square" '
        'd="M5.5 2.5H8.5L10.5 4.5V7.5L8.5 9.5H5.5L3.5 7.5V4.5Z"/>'
        f"{handle}"
        f'<path fill="{TEAL_ON_DARK}" d="M5 6h1v1H5zM6 5h1v1H6zM7 6h1v1H7zM8 5h1v1H8z"/>'
        "</g>"
    )


def svg(width: float, height: float, body: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_ntos(width)}" height="{_ntos(height)}" '
        f'viewBox="0 0 {_ntos(width)} {_ntos(height)}" role="img" aria-label="{title}">'
        f"<title>{title}</title>{body}</svg>\n"
    )


def lockup(fonts: dict[str, TTFont], fg: str, accent: str) -> str:
    size, gap, track = 40.0, 14.0, -0.6
    eb = fonts["ExtraBold"]
    baseline = 32 + x_height(eb, size) / 2
    d1, w1 = text_path(eb, "traced", size, 64 + gap, baseline, tracking=track)
    d2, w2 = text_path(eb, "research", size, 64 + gap + w1 + track, baseline, tracking=track)
    width = 64 + gap + w1 + track + w2 + 2
    body = mark(fg, accent) + f'<path fill="{fg}" d="{d1}"/><path fill="{accent}" d="{d2}"/>'
    return svg(width, 64, body, "tracedresearch")


def social(fonts: dict[str, TTFont]) -> str:
    W, H, left = 1280, 640, 96
    eb, md = fonts["ExtraBold"], fonts["Medium"]
    parts = [f'<rect width="{W}" height="{H}" fill="{INK}"/>']
    parts.append(mark(MIST, TEAL_ON_DARK, x=left - 8, y=84, size=120))
    wsize = 76
    base = 84 + 60 + x_height(eb, wsize) / 2
    d1, w1 = text_path(eb, "traced", wsize, left + 136, base, tracking=-1.2)
    d2, _ = text_path(eb, "research", wsize, left + 136 + w1 - 1.2, base, tracking=-1.2)
    parts.append(f'<path fill="{MIST}" d="{d1}"/><path fill="{TEAL_ON_DARK}" d="{d2}"/>')
    for i, line in enumerate(("Deep research that survives", "a crash, and shows its work.")):
        d, _ = text_path(md, line, 52, left, 318 + i * 66)
        parts.append(f'<path fill="{MIST}" d="{d}"/>')
    d, _ = text_path(md, "Redis-durable LangGraph · hard budgets in code · validated citations", 25, left, 452)
    parts.append(f'<path fill="{MUTED_ON_DARK}" d="{d}"/>')
    d, _ = text_path(md, "· step-level trace · resume after a hard kill, no replay", 25, left, 488)
    parts.append(f'<path fill="{MUTED_ON_DARK}" d="{d}"/>')
    d, _ = text_path(md, "github.com/niravpatidar37/tracedresearch", 24, left, 566)
    parts.append(f'<path fill="{TEAL_ON_DARK}" d="{d}"/>')
    return svg(W, H, "".join(parts), "tracedresearch: deep research that survives a crash, and shows its work")


def avatar() -> str:
    """Opaque square avatar: the mark centred on the ink tile, no rounded clip."""
    return f'<rect width="512" height="512" fill="{INK}"/>' + mark(MIST, TEAL_ON_DARK, x=72, y=72, size=368)


# ----------------------------------------------------------------- safety gate

FORBIDDEN = [
    (re.compile(r"<\s*script", re.I), "script element"),
    (re.compile(r"\son[a-z]+\s*=", re.I), "event handler attribute"),
    (re.compile(r"<\s*foreignObject", re.I), "foreignObject"),
    (re.compile(r"(?:xlink:)?href\s*=\s*\"(?!#)", re.I), "external reference"),
    (re.compile(r"url\(\s*['\"]?(?!#)", re.I), "external url()"),
    (re.compile(r"javascript:", re.I), "javascript: URL"),
]


def check_safe(name: str, text: str) -> None:
    for pattern, what in FORBIDDEN:
        if pattern.search(text):
            sys.exit(f"{name}: unsafe SVG content ({what})")


# ----------------------------------------------------------------- main


def main() -> None:
    checks = {
        "accent on GitHub light": contrast(TEAL_ON_LIGHT, GITHUB_LIGHT_BG),
        "accent on GitHub dark": contrast(TEAL_ON_DARK, GITHUB_DARK_BG),
        "ink on GitHub light": contrast(INK, GITHUB_LIGHT_BG),
        "mist on GitHub dark": contrast(MIST, GITHUB_DARK_BG),
        "favicon trace on tile": contrast(TEAL_ON_DARK, INK),
        "muted text on social": contrast(MUTED_ON_DARK, INK),
    }
    for label, ratio in checks.items():
        print(f"contrast {label}: {ratio:.2f}:1")
        if ratio < 3.0:
            sys.exit(f"contrast below WCAG 3:1 for graphics: {label}")

    fonts = load_fonts()
    files = {
        "icon.svg": svg(64, 64, mark(INK, TEAL_ON_LIGHT), "tracedresearch"),
        "icon-dark.svg": svg(64, 64, mark(MIST, TEAL_ON_DARK), "tracedresearch"),
        "favicon.svg": svg(32, 32, favicon_mark(), "tracedresearch"),
        "favicon-16.svg": svg(16, 16, favicon16_mark(), "tracedresearch"),
        "avatar.svg": svg(512, 512, avatar(), "tracedresearch"),
        "logo.svg": lockup(fonts, INK, TEAL_ON_LIGHT),
        "logo-dark.svg": lockup(fonts, MIST, TEAL_ON_DARK),
        "social-preview.svg": social(fonts),
    }
    for name, text in files.items():
        check_safe(name, text)
        (OUT / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {name} ({len(text)} bytes)")

    resvg = shutil.which("resvg")
    if not resvg:
        sys.exit("resvg not found on PATH; install it to export PNGs")
    renders = [
        ("favicon-16.svg", "favicon-16.png", 16),
        ("favicon.svg", "favicon-32.png", 32),
        ("avatar.svg", "avatar-512.png", 512),
        ("social-preview.svg", "social-preview.png", 1280),
    ]
    for src, dst, width in renders:
        # List-form argv, no shell: all arguments are constants from `renders`
        # plus the absolute resvg path from shutil.which.
        argv = [resvg, "-w", str(width), str(OUT / src), str(OUT / dst)]
        subprocess.run(argv, check=True, shell=False)
        print(f"rendered {dst} ({(OUT / dst).stat().st_size} bytes)")


if __name__ == "__main__":
    main()
