"""Карточки постов (PNG) по одному шаблону.

Нужен Pillow (`pip install -e '.[channel]'`) и шрифт с кириллицей: путь в PARKAN_FONT
(обычный) и PARKAN_FONT_BOLD (жирный), иначе ищется DejaVu Sans. Если чего-то нет,
карточки не рисуются, а черновики уходят текстом.
"""

from __future__ import annotations

import os
from pathlib import Path

W, H = 1200, 675
BG, FG, MUTED, ACCENT, DARK = "#0F1B2D", "#FFFFFF", "#9FB0C8", "#FFB020", "#0F1B2D"
PAD = 64

FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/Library/Fonts",
             "/System/Library/Fonts/Supplemental", "C:/Windows/Fonts")


def _font_path(bold: bool) -> str | None:
    env = os.environ.get("PARKAN_FONT_BOLD" if bold else "PARKAN_FONT")
    if env and Path(env).exists():
        return env
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in FONT_DIRS:
        p = Path(d) / name
        if p.exists():
            return str(p)
    return None


def available() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return bool(_font_path(False) and _font_path(True))


def _wrap(draw, text: str, font, width: int, max_lines: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = f"{cur} {w}".strip()
        if draw.textlength(t, font=font) <= width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(",") + "…"
    return lines


def _fit(draw, text: str, path: str, size: int, width: int):
    from PIL import ImageFont
    while size > 24:
        f = ImageFont.truetype(path, size)
        if draw.textlength(text, font=f) <= width:
            return f
        size -= 4
    return ImageFont.truetype(path, size)


def render(card: dict, out_path, channel: str) -> Path:
    """card: label, big, sub, place, district, badge (см. templates.*_card)."""
    from PIL import Image, ImageDraw, ImageFont
    regular, bold = _font_path(False), _font_path(True)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    width = W - 2 * PAD

    small = ImageFont.truetype(bold, 30)
    d.text((PAD, PAD), card.get("label", ""), font=small, fill=ACCENT)
    ch = ImageFont.truetype(regular, 28)
    d.text((W - PAD - d.textlength(channel, font=ch), PAD + 2), channel, font=ch, fill=MUTED)

    y = PAD + 70
    big = _fit(d, card["big"], bold, 150, width)
    d.text((PAD, y), card["big"], font=big, fill=FG)
    y += big.size + 30
    sub = _fit(d, card.get("sub", ""), regular, 42, width)
    d.text((PAD, y), card.get("sub", ""), font=sub, fill=FG)
    y += sub.size + 36

    place = ImageFont.truetype(regular, 34)
    text = card.get("place", "") + (f" · {card['district']}" if card.get("district") else "")
    for line in _wrap(d, text, place, width, 2):
        d.text((PAD, y), line, font=place, fill=MUTED)
        y += 46

    if card.get("badge"):
        bf = ImageFont.truetype(bold, 30)
        tw = d.textlength(card["badge"], font=bf)
        x0, y0 = PAD, H - PAD - 56
        d.rounded_rectangle((x0, y0, x0 + tw + 40, y0 + 56), radius=28, fill=ACCENT)
        d.text((x0 + 20, y0 + 10), card["badge"], font=bf, fill=DARK)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG", optimize=True)
    return out
