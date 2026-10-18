#!/usr/bin/env python3
"""Add window chrome (padding, three dots, rounded corners) to an agg gif.
Usage: frame.py raw.gif demo.gif [title]"""

import sys

from PIL import Image, ImageDraw, ImageFont

src, dst = sys.argv[1], sys.argv[2]
title = sys.argv[3] if len(sys.argv) > 3 else ""
BG, PAD, BAR, RADIUS = (10, 20, 24), 28, 48, 20  # brand Ink #0A1418
DOTS = [(255, 95, 87), (254, 188, 46), (40, 200, 64)]

im = Image.open(src)
w, h = im.size
W, H = w + 2 * PAD, h + BAR + PAD


def font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/segoeui.ttf"):
        try:
            return ImageFont.truetype(path, 18)
        except OSError:
            continue
    return ImageFont.load_default()


FONT = font()


def canvas() -> Image.Image:
    c = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(c)
    d.rounded_rectangle((0, 0, W - 1, H - 1), RADIUS, fill=(*BG, 255))
    for i, col in enumerate(DOTS):
        x = PAD + i * 26
        d.ellipse((x, BAR // 2 - 8, x + 16, BAR // 2 + 8), fill=col)
    if title:
        tw = d.textlength(title, font=FONT)
        d.text(((W - tw) / 2, BAR // 2 - 12), title, font=FONT, fill=(147, 168, 172))
    return c


frames, durations = [], []
for i in range(im.n_frames):
    im.seek(i)
    c = canvas()
    c.alpha_composite(im.convert("RGBA"), (PAD, BAR))
    frames.append(c)
    durations.append(im.info.get("duration", 40))

# One shared palette (255 colours + 1 transparent) so frames don't flicker.
sample = Image.new("RGB", (W, 3 * H))
for k, idx in enumerate((0, len(frames) // 2, len(frames) - 1)):
    sample.paste(frames[idx].convert("RGB"), (0, k * H))
pal = sample.quantize(255, method=Image.Quantize.MEDIANCUT)
out = []
for fr in frames:
    rgb = fr.convert("RGB").quantize(palette=pal, dither=Image.Dither.NONE)
    mask = fr.getchannel("A").point(lambda a: 255 if a < 128 else 0)
    rgb.paste(255, mask=mask)  # index 255 = transparent (outside the corners)
    out.append(rgb)
out[0].save(
    dst, save_all=True, append_images=out[1:], duration=durations, loop=0, transparency=255, disposal=1, optimize=False
)
print(dst, out[0].size, len(out), "frames", sum(durations) / 1000, "s")
