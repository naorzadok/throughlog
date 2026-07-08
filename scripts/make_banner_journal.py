"""Regenerate the alternate README banner (assets/banner_journal.png).

One-off asset generator, not part of the product: needs Pillow (`pip install pillow`).
The visual is deliberately wordless — it has to land "the self-writing journal"
on sight: the loose ideas of a workday (small jewel-toned sparks — stars,
triangles, diamonds, rings) are entrained into streams, funnel through a gold
gate (the privacy gate), and settle onto a bright journal page as calm,
abstract text lines — the last one still being inked under a glowing caret.
Scattered ideas in, a written day out, no reading required.
Only the wordmark + tagline carry words. Same Space Grotesk (vendored in
assets/fonts/) and palette as the hero banner. Rendered at 2x (1840x520).

    python scripts/make_banner_journal.py
"""
from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "banner_journal.png"
FONT = ROOT / "assets" / "fonts" / "SpaceGrotesk-Variable.ttf"

# ----- canvas ---------------------------------------------------------------
W, H = 1840, 520
CORNER = 28
SEED = 20260707                  # fixed so the scatter is reproducible

BG_TOP = (23, 27, 40)
BG_BOT = (10, 12, 20)

# ----- type / palette (matches make_banner.py) -------------------------------
IVORY = (233, 228, 214)
IVORY_LO = (206, 200, 184)
GOLD_HI = (243, 199, 107)
GOLD_LO = (200, 134, 58)
MUTED = (150, 160, 181)
GOLD_RULE = (214, 164, 90)
SLATE = (124, 134, 156)

TAGLINE = "The self-writing journal for your workday - and your AI agents."
TAGLINE_SHORT = "The self-writing journal for your work - and your AI agents."

# ----- the journal page (right) ----------------------------------------------
P_X0, P_Y0, P_X1, P_Y1 = 1270, 100, 1756, 432
P_PAD = 44
PAGE_TOP = (240, 235, 222)       # warm ivory, brightest thing on the banner
PAGE_BOT = (222, 216, 200)
BAR = (92, 102, 128)             # abstract "text" bars on the page
FOLD = 46                        # dog-ear corner size

# paragraph skeleton: (width fraction, extra gap above); the page reads as prose
ROWS = [
    (1.00, 0), (0.87, 0), (0.94, 0), (0.58, 0),      # first paragraph
    (0.90, 14),                                       # new paragraph
]
WRITING_FRAC = 0.42              # the line being written right now (gold)

# ----- the day's debris (left) + streams into the gate ------------------------
CHAOS_X = (100, 640)
CHAOS_Y = (272, 452)
STREAMS = [                      # bezier starts inside the chaos field
    (150, 322), (255, 436), (372, 288), (486, 412),
]
GATE_R = 30                      # the gold ring the streams pass through

# the day's ideas: small shapes in darkened jewel tones that stay calm on navy
IDEA_HUES = [
    (176, 96, 100),              # muted red
    (110, 152, 114),             # sage green
    (96, 146, 154),              # teal
    (134, 112, 168),             # violet
    (102, 128, 174),             # steel blue
]
N_IDEAS = 34
MIN_DIST = 36                    # rejection-sampling spacing: no clumps, no collisions


def grotesk(size: int, weight: int) -> ImageFont.FreeTypeFont:
    """Space Grotesk at a given size and weight (variable wght axis, 300-700)."""
    f = ImageFont.truetype(str(FONT), size)
    f.set_variation_by_axes([weight])
    return f


def vgrad(size: tuple[int, int], top: tuple, bot: tuple) -> Image.Image:
    """Vertical RGB gradient."""
    w, h = size
    col = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        col.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(top, bot)))
    return col.resize((w, h))


def radial_mask(size: int, blur: int) -> Image.Image:
    """Soft radial blob (white center -> black edge), used for glows."""
    m = Image.new("L", (size, size), 0)
    pad = blur * 2
    ImageDraw.Draw(m).ellipse((pad, pad, size - pad, size - pad), fill=255)
    return m.filter(ImageFilter.GaussianBlur(blur))


def glow_blob(banner: Image.Image, cx: int, cy: int, size: int, color: tuple) -> None:
    blob = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    blob.paste(Image.new("RGBA", (size, size), color), (0, 0),
               radial_mask(size, size // 8))
    banner.alpha_composite(blob, (cx - size // 2, cy - size // 2))


def gradient_text(text: str, font: ImageFont.FreeTypeFont, top: tuple, bot: tuple) -> Image.Image:
    """Render text filled with a vertical gradient; returns RGBA image."""
    l, t, r, b = font.getbbox(text)
    tw, th = r - l, b - t
    mask = Image.new("L", (tw, th), 0)
    ImageDraw.Draw(mask).text((-l, -t), text, font=font, fill=255)
    grad = vgrad((tw, th), top, bot)
    out = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def bez(p0, p1, p2, p3, t: float) -> tuple[float, float]:
    u = 1 - t
    return (
        u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
        u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1],
    )


def draw_shape(draw: ImageDraw.ImageDraw, kind: str, x: float, y: float,
               r: float, col: tuple, rot: float = 0.0) -> None:
    """One small "idea" glyph: a sparkle star, triangle, diamond, ring or dot."""
    if kind == "star":                               # 4-point sparkle
        pts = []
        for i in range(8):
            ang = rot + math.pi * i / 4
            rad = r if i % 2 == 0 else r * 0.36
            pts.append((x + rad * math.sin(ang), y - rad * math.cos(ang)))
        draw.polygon(pts, fill=col)
    elif kind == "tri":
        pts = [(x + r * math.sin(rot + 2 * math.pi * i / 3),
                y - r * math.cos(rot + 2 * math.pi * i / 3)) for i in range(3)]
        draw.polygon(pts, fill=col)
    elif kind == "diamond":
        pts = [(x + r * math.sin(rot + math.pi * i / 2),
                y - r * math.cos(rot + math.pi * i / 2)) for i in range(4)]
        draw.polygon(pts, fill=col)
    elif kind == "ring":
        draw.ellipse((x - r, y - r, x + r, y + r), outline=col, width=2)
    else:                                            # dot
        draw.ellipse((x - r, y - r, x + r, y + r), fill=col)


def main() -> None:
    rnd = random.Random(SEED)

    # -- background: gradient + warm glow behind the page + grain --
    banner = vgrad((W, H), BG_TOP, BG_BOT).convert("RGBA")
    glow_blob(banner, (P_X0 + P_X1) // 2, (P_Y0 + P_Y1) // 2, 1200, (226, 152, 62, 20))

    noise = Image.effect_noise((W, H), 12).convert("L")
    banner = Image.composite(
        Image.new("RGBA", (W, H), (255, 255, 255, 255)), banner,
        noise.point(lambda v: 3 if v > 128 else 0),
    )
    draw = ImageDraw.Draw(banner)

    # the writing line's y on the page decides where every stream lands
    bar_h, pitch = 13, 33
    rows_y, y = [], P_Y0 + P_PAD + 12 + 30           # below the gold date bar
    for _, gap in ROWS:
        y += gap
        rows_y.append(y)
        y += pitch
    wy = rows_y[-1] + pitch + 14 + bar_h // 2        # center of the gold writing bar
    gate_cx = P_X0 - 80

    # -- the day's ideas: small jewel-toned shapes, denser on the left --
    # rejection-sampled so nothing clumps or collides; seeded with the stream
    # starts so the anchor sparks (below) keep clear space around them
    placed: list[tuple[float, float]] = [(float(sx), float(sy)) for sx, sy in STREAMS]
    field: list[tuple[str, float, float, float, tuple, float]] = []
    tries = 0
    while len(field) < N_IDEAS and tries < 4000:
        tries += 1
        x = rnd.uniform(*CHAOS_X)
        yy = rnd.uniform(*CHAOS_Y)
        if rnd.random() < (x - CHAOS_X[0]) / (CHAOS_X[1] - CHAOS_X[0]) * 0.55:
            continue                                 # thin out toward the right
        if any((x - px) ** 2 + (yy - py) ** 2 < MIN_DIST ** 2 for px, py in placed):
            continue
        hue = GOLD_RULE if rnd.random() < 0.10 else IDEA_HUES[rnd.randrange(len(IDEA_HUES))]
        kind = rnd.random()
        if kind < 0.30:
            k, r, a = "star", rnd.uniform(5.0, 9.0), rnd.randint(150, 210)
        elif kind < 0.50:
            k, r, a = "tri", rnd.uniform(4.0, 6.5), rnd.randint(120, 180)
        elif kind < 0.68:
            k, r, a = "diamond", rnd.uniform(3.5, 5.5), rnd.randint(120, 180)
        elif kind < 0.82:
            k, r, a = "ring", rnd.uniform(3.5, 5.5), rnd.randint(110, 170)
        else:
            k, r, a = "dot", rnd.uniform(1.8, 2.8), rnd.randint(100, 160)
        placed.append((x, yy))
        field.append((k, x, yy, r, hue + (a,), rnd.uniform(0, math.tau)))

    for k, x, yy, r, col, rot in field:              # soft halos under the big stars
        if k == "star" and r > 7:
            glow_blob(banner, int(x), int(yy), int(r * 9), col[:3] + (26,))
    draw = ImageDraw.Draw(banner)
    for k, x, yy, r, col, rot in field:
        draw_shape(draw, k, x, yy, r, col, rot)

    # anchor sparks: one brighter star at each stream start — the ideas being
    # picked up right now
    for (sx, sy), hue in zip(STREAMS, IDEA_HUES):
        glow_blob(banner, sx, sy, 96, hue + (48,))
    draw = ImageDraw.Draw(banner)
    for (sx, sy), hue in zip(STREAMS, IDEA_HUES):
        draw_shape(draw, "star", sx, sy, 9.5, hue + (235,))

    # -- streams: debris entrained toward the gate, then onto the page --
    curves = [((sx, sy), (sx + 260, sy + rnd.randint(-40, 30)),
               (1060, wy + rnd.randint(-12, 12)), (P_X0 - 6, wy))
              for sx, sy in STREAMS]

    # no drawn paths — pure particle drift; the jitter tightens toward the gate
    # (loose cloud at the start, focused beam at the end: chaos becoming order)
    for c in curves:
        t = 0.04
        while t < 0.99:
            x, yy = bez(*c, t)
            j = 1 + 7 * (1 - t)
            x += rnd.uniform(-j, j)
            yy += rnd.uniform(-j, j)
            past_gate = x > gate_cx
            a = int(40 + 125 * t ** 1.2) + (40 if past_gate else 0)
            r = 1.6 + 2.1 * t
            if past_gate or rnd.random() < 0.05 + 0.30 * t * t:
                rgb = GOLD_RULE                      # distilled: gold from here on
            elif t < 0.45 and rnd.random() < 0.6 * (1 - t / 0.45):
                rgb = IDEA_HUES[rnd.randrange(len(IDEA_HUES))]   # still carrying its color
            else:
                rgb = (108, 118, 140)
            col = rgb + (min(210, a),)
            draw.ellipse((x - r, yy - r, x + r, yy + r), fill=col)
            t += rnd.uniform(0.019, 0.034)

    # a few ideas caught mid-flight, shrinking as they near the gate
    for _ in range(9):
        c = curves[rnd.randrange(len(curves))]
        t = rnd.uniform(0.18, 0.60)
        x, yy = bez(*c, t)
        x += rnd.uniform(-8, 8)
        yy += rnd.uniform(-14, 14)
        hue = IDEA_HUES[rnd.randrange(len(IDEA_HUES))]
        r = max(2.4, 6.2 - 5.0 * t)
        k = ("star", "tri", "diamond")[rnd.randrange(3)]
        draw_shape(draw, k, x, yy, r, hue + (rnd.randint(70, 130),),
                   rot=rnd.uniform(0, math.tau))

    # -- the gate: a thin gold ring every stream passes through --
    glow_blob(banner, gate_cx, wy, 170, (226, 152, 62, 60))
    draw = ImageDraw.Draw(banner)
    draw.ellipse((gate_cx - GATE_R, wy - GATE_R, gate_cx + GATE_R, wy + GATE_R),
                 outline=GOLD_RULE + (170,), width=3)

    # -- the page: bright ivory card with a folded corner, the eye magnet --
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (P_X0 - 4, P_Y0 + 12, P_X1 + 10, P_Y1 + 22), radius=22, fill=(0, 0, 0, 130))
    banner.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(24)))

    pw, ph = P_X1 - P_X0, P_Y1 - P_Y0
    page_mask = Image.new("L", (pw, ph), 0)
    pm = ImageDraw.Draw(page_mask)
    pm.rounded_rectangle((0, 0, pw - 1, ph - 1), radius=18, fill=255)
    pm.polygon([(pw - FOLD, 0), (pw, 0), (pw, FOLD)], fill=0)   # clip the dog-ear
    page = vgrad((pw, ph), PAGE_TOP, PAGE_BOT).convert("RGBA")
    page.putalpha(page_mask)
    banner.alpha_composite(page, (P_X0, P_Y0))
    draw = ImageDraw.Draw(banner)

    # folded corner flap + its crease shadow
    draw.polygon([(P_X1 - FOLD, P_Y0), (P_X1, P_Y0 + FOLD), (P_X1 - FOLD, P_Y0 + FOLD)],
                 fill=(211, 203, 184, 255))
    draw.line((P_X1 - FOLD, P_Y0, P_X1, P_Y0 + FOLD), fill=(60, 66, 88, 60), width=2)

    # -- the written day: gold date bar, calm prose bars, one line mid-ink --
    cx0, cx1 = P_X0 + P_PAD, P_X1 - P_PAD
    span = cx1 - cx0
    date_y = P_Y0 + P_PAD
    draw.rounded_rectangle((cx0, date_y, cx0 + 132, date_y + 12), radius=6,
                           fill=GOLD_LO + (255,))

    for (frac, _), ry in zip(ROWS, rows_y):
        draw.rounded_rectangle((cx0, ry, cx0 + int(span * frac), ry + bar_h),
                               radius=6, fill=BAR + (235,))

    # the writing bar: gold, brightening toward the caret (fresh ink)
    wb_w = int(span * WRITING_FRAC)
    wb_y = wy - bar_h // 2
    wb_mask = Image.new("L", (wb_w, bar_h), 0)
    ImageDraw.Draw(wb_mask).rounded_rectangle((0, 0, wb_w - 1, bar_h - 1), radius=6, fill=255)
    wb = vgrad((bar_h, wb_w), GOLD_LO, GOLD_HI).rotate(-90, expand=True).convert("RGBA")
    wb.putalpha(wb_mask)
    banner.alpha_composite(wb, (cx0, wb_y))

    caret_x = cx0 + wb_w + 12
    glow_blob(banner, caret_x + 3, wy, 80, (226, 152, 62, 95))
    draw = ImageDraw.Draw(banner)
    draw.rounded_rectangle((caret_x, wy - 17, caret_x + 7, wy + 17), radius=3,
                           fill=(186, 124, 48, 255))

    # -- wordmark + tagline, top-left; the only words on the banner --
    tx, word_y = 84, 64
    f_word = grotesk(152, 700)
    part1 = gradient_text("Through", f_word, IVORY, IVORY_LO)
    part2 = gradient_text("Log", f_word, GOLD_HI, GOLD_LO)
    banner.alpha_composite(part1, (tx, word_y))
    banner.alpha_composite(part2, (tx + part1.width + 8, word_y))

    avail = P_X0 - tx - 40
    tagline, f_tag = TAGLINE, None
    for size in range(42, 31, -1):
        f = grotesk(size, 400)
        if draw.textlength(TAGLINE, font=f) <= avail:
            f_tag = f
            break
    if f_tag is None:
        tagline, f_tag = TAGLINE_SHORT, grotesk(34, 400)
    _, tt, _, _ = f_tag.getbbox(tagline)
    draw.text((tx, word_y + max(part1.height, part2.height) + 30 - tt),
              tagline, font=f_tag, fill=MUTED)

    # -- vignette + rounded corners --
    vin = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vin).rounded_rectangle((-160, -160, W + 160, H + 160), radius=400, fill=255)
    vin = vin.filter(ImageFilter.GaussianBlur(120))
    dark = Image.new("RGBA", (W, H), (5, 6, 12, 255))
    banner = Image.composite(banner, dark, vin.point(lambda v: 155 + v * 100 // 255))

    corner_mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(corner_mask).rounded_rectangle((0, 0, W, H), radius=CORNER, fill=255)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.paste(banner, (0, 0), corner_mask)

    out.save(OUT, optimize=True)
    print(f"wrote {OUT} {out.size} | tagline @ {f_tag.size}px | writing line y={wy}")


if __name__ == "__main__":
    main()
