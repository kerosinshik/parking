"""Карточки по макету из Claude Design (тёмный изумруд, лайм): HTML → PNG через headless Chromium.

Макет и токены — docs/design/handoff/README.md. Шрифты, иллюстрации и границы районов (OSM, ODbL)
лежат в parkan/channel/assets, сеть при отрисовке не нужна. Карта Москвы считается здесь же,
в Python, и вставляется в страницу готовым SVG — JavaScript странице не нужен.

Браузер: PARKAN_CHROME, иначе Chromium Playwright (/opt/pw-browsers) или chromium/google-chrome из PATH.
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from html import escape
from pathlib import Path

ASSETS = Path(__file__).parent / "assets"
W, H = 560, 600                      # кадр карты
OUTER = ("ЗелАО", "НАО", "ТАО")
K = math.cos(math.radians(55.75))

BG, LIME, LIGHT, SUB, MUTED, RULE = "#0E3B2E", "#C8F04A", "#F6F4EE", "#CFE0D6", "#A9C4B6", "#2B5A49"
OKRUG_FILL, OTHER_FILL = "#3A7A60", "#1A4F3F"

ILLUSTRATIONS = {"parking": ("parking.png", 1.9), "flat": ("flat.png", 1.0)}


# ---------------------------------------------------------------- браузер

def chrome_path() -> str | None:
    env = os.environ.get("PARKAN_CHROME")
    if env and Path(env).exists():
        return env
    for p in sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"), reverse=True):
        return p
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        if shutil.which(name):
            return shutil.which(name)
    return None


def available() -> bool:
    return chrome_path() is not None


# ---------------------------------------------------------------- карта

@lru_cache(maxsize=1)
def _districts() -> list[dict]:
    return json.loads((ASSETS / "moscow-geo.json").read_text(encoding="utf-8"))["districts"]


def _norm(s: str | None) -> str:
    s = (s or "").lower().replace("ё", "е").replace("район", "")
    return re.sub(r"[^a-zа-я0-9]+", "", s)


def find_district(name: str | None) -> dict | None:
    key = _norm(name)
    return next((d for d in _districts() if _norm(d["n"]) == key), None) if key else None


def district_line(name: str | None) -> str:
    """«Бескудниковский район · САО»; район не найден — исходная строка без округа."""
    hit = find_district(name)
    if not hit:
        return name or ""
    n = hit["n"]
    return f"{n if n.endswith('район') else 'район ' + n} · {hit['o']}"


def _main_ring(d: dict) -> list:
    def area(r):
        return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(r, r[1:] + r[:1])))
    return max(d["r"], key=area)


def map_svg(district: str | None) -> str:
    """Москва по районам: район лота — лайм, его округ — светлее, остальное — фон; кадр по округу."""
    ds = _districts()
    hit = find_district(district)
    if not hit or hit["o"] not in OUTER:
        shown = [d for d in ds if d["o"] not in OUTER]
    elif hit["o"] == "ЗелАО":
        shown = [d for d in ds if d["o"] not in ("НАО", "ТАО")]
    else:
        shown = [d for d in ds if d["o"] != "ЗелАО"]
    focus = [d for d in ds if d["o"] == hit["o"]] if hit else shown
    # кадр — по главному контуру каждого района: у «Восточного» есть анклав за 20 км, он мельчил бы карту
    xs = [lo * K for d in focus for lo, la in _main_ring(d)]
    ys = [-la for d in focus for lo, la in _main_ring(d)]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    span = max((max(xs) - min(xs)) / W, (max(ys) - min(ys)) / H, 0.24 * K / W) * 1.6
    s = 1 / span

    def path(d):
        return "".join("M" + "L".join(f"{W / 2 + (lo * K - cx) * s:.1f},{H / 2 + (-la - cy) * s:.1f}" for lo, la in r) + "Z"
                       for r in d["r"])

    def layer(d):
        return 2 if d is hit else 1 if hit and d["o"] == hit["o"] else 0
    fills = {0: OTHER_FILL, 1: OKRUG_FILL, 2: LIME}
    paths = [f'<path d="{path(d)}" fill="{fills[layer(d)]}"/>' for d in sorted(shown, key=layer)]
    return (f'<svg viewBox="0 0 {W} {H}" width="{W}" height="{H}" class="map">'
            f'<g stroke="{BG}" stroke-width="1.3" stroke-linejoin="round">{"".join(paths)}</g></svg>')


# ---------------------------------------------------------------- страница

def _fonts_css() -> str:
    faces = (("Unbounded", 700, "Unbounded-Bold"), ("Unbounded", 800, "Unbounded-ExtraBold"),
             ("Onest", 500, "Onest-Medium"), ("Onest", 600, "Onest-SemiBold"), ("Onest", 700, "Onest-Bold"))
    return "".join(f"@font-face{{font-family:{f};font-weight:{w};src:url('{(ASSETS / 'fonts' / (n + '.ttf')).as_uri()}')}}"
                   for f, w, n in faces)


CSS = f"""
*{{box-sizing:border-box}} html,body{{margin:0;background:{BG}}}
.card{{width:1080px;height:1080px;background:{BG};position:relative;overflow:hidden;font-family:Onest,sans-serif;
  padding:68px 76px;display:flex;flex-direction:column}}
.mapbox{{position:absolute;right:24px;top:128px;width:{W}px;height:{H}px}}
.map{{display:block;overflow:hidden;-webkit-mask-image:radial-gradient(closest-side,#000 62%,transparent 100%);
  mask-image:radial-gradient(closest-side,#000 62%,transparent 100%)}}
.top{{display:flex;justify-content:space-between;align-items:center;position:relative}}
.kicker{{background:{LIME};color:{BG};font:700 30px Onest;letter-spacing:.04em;padding:12px 26px;border-radius:40px}}
.brand{{font:700 28px Unbounded;color:{LIGHT};letter-spacing:.06em}}
.head{{position:relative;margin-top:110px;max-width:470px;display:flex;flex-direction:column}}
.headline{{font:800 132px/1 Unbounded;color:{LIME};letter-spacing:-.04em;white-space:nowrap}}
.headsub{{font:500 36px/1.2 Onest;color:{SUB};margin-top:22px;text-wrap:pretty}}
.pill{{align-self:flex-start;margin-top:32px;border:2px solid {LIME};color:{LIME};font:600 28px Onest;
  padding:10px 22px;border-radius:40px}}
.bottom{{margin-top:auto;position:relative;border-top:2px solid {RULE};padding-top:40px;display:grid;
  grid-template-columns:230px minmax(0,1fr);column-gap:48px;align-items:center}}
.illus{{width:230px;height:230px;object-fit:contain;display:block}}
.info{{display:flex;flex-direction:column;min-width:0}}
.price{{font:700 74px/1 Unbounded;color:{LIGHT};letter-spacing:-.03em;white-space:nowrap}}
.pricesub{{font:500 30px/1.25 Onest;color:{MUTED};margin-top:14px}}
.addr{{font:700 34px/1.2 Onest;color:{LIGHT};margin-top:26px;text-wrap:pretty;display:-webkit-box;
  -webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.district{{font:500 28px Onest;color:{MUTED};margin-top:8px}}
"""


def lot_html(c: dict) -> str:
    """c: kicker, brand, headline, headline_sub, deadline, price, price_sub, address, district, type."""
    img, scale = ILLUSTRATIONS.get(c.get("type") or "parking", ILLUSTRATIONS["parking"])
    e = lambda k: escape(str(c.get(k) or ""))   # noqa: E731
    pill = f'<div class="pill">{e("deadline")}</div>' if c.get("deadline") else ""
    sub = f'<div class="headsub">{e("headline_sub")}</div>' if c.get("headline_sub") else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{_fonts_css()}{CSS}</style></head><body>
<div class="card">
  <div class="mapbox">{map_svg(c.get("district"))}</div>
  <div class="top"><div class="kicker">{e("kicker")}</div><div class="brand">{e("brand")}</div></div>
  <div class="head"><div class="headline">{e("headline")}</div>{sub}{pill}</div>
  <div class="bottom">
    <img class="illus" src="{(ASSETS / img).as_uri()}" style="transform:scale({scale})">
    <div class="info">
      <div class="price">{e("price")}</div>
      <div class="pricesub">{e("price_sub")}</div>
      <div class="addr">{e("address")}</div>
      <div class="district">{escape(district_line(c.get("district")))}</div>
    </div>
  </div>
</div></body></html>"""


def render_html(html: str, out_path, chrome: str | None = None) -> Path:
    """Скриншот страницы 1080×1080 в PNG."""
    chrome = chrome or chrome_path()
    if not chrome:
        raise RuntimeError("нет Chromium для отрисовки карточек (PARKAN_CHROME)")
    from PIL import Image
    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        page, shot = Path(tmp) / "card.html", Path(tmp) / "shot.png"
        page.write_text(html, encoding="utf-8")
        # окно выше карточки: у headless-режима видимая область меньше окна, низ иначе обрезается
        subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                        "--force-device-scale-factor=1", "--window-size=1080,1400", "--virtual-time-budget=3000",
                        "--allow-file-access-from-files", f"--user-data-dir={tmp}/profile",
                        f"--screenshot={shot}", page.as_uri()],
                       check=True, capture_output=True, timeout=90)
        if not shot.exists():
            raise RuntimeError("Chromium не сохранил карточку")
        Image.open(shot).convert("RGB").crop((0, 0, 1080, 1080)).save(out, "PNG", optimize=True)
    return out
