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
MAX_SPAN = 0.36 * K / W          # самый широкий кадр для одного района, градусов на пиксель

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


def map_svg(district) -> str:
    """Москва по районам: районы лотов — лайм, их округа — светлее, остальное — фон; кадр по округам.

    district — название района или список названий (для подборок).
    """
    ds = _districts()
    names = [district] if isinstance(district, str) or district is None else list(district)
    hits = [h for h in (find_district(n) for n in names) if h]
    hit_ids = {id(h) for h in hits}
    okrugs = {h["o"] for h in hits}
    if not okrugs & set(OUTER):
        shown = [d for d in ds if d["o"] not in OUTER]
    elif okrugs & set(OUTER) == {"ЗелАО"}:
        shown = [d for d in ds if d["o"] not in ("НАО", "ТАО")]
    elif "ЗелАО" not in okrugs:
        shown = [d for d in ds if d["o"] != "ЗелАО"]
    else:
        shown = list(ds)
    focus = [d for d in ds if d["o"] in okrugs] if hits else shown
    # кадр — по главному контуру каждого района: у «Восточного» есть анклав за 20 км, он мельчил бы карту
    xs = [lo * K for d in focus for lo, la in _main_ring(d)]
    ys = [-la for d in focus for lo, la in _main_ring(d)]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    span = max((max(xs) - min(xs)) / W, (max(ys) - min(ys)) / H, 0.24 * K / W)
    if len(hits) == 1 and span > MAX_SPAN:
        # округ с дальними районами (ЗАО с Внуково и т. п.): не мельчим, а центрируем на районе лота
        ring = _main_ring(hits[0])
        cx = (min(lo for lo, la in ring) + max(lo for lo, la in ring)) / 2 * K
        cy = -(min(la for lo, la in ring) + max(la for lo, la in ring)) / 2
        span = MAX_SPAN
    s = 1 / (span * (1.6 if len(hits) <= 1 else 1.15))   # подборка — вся Москва крупнее

    def path(d):
        return "".join("M" + "L".join(f"{W / 2 + (lo * K - cx) * s:.1f},{H / 2 + (-la - cy) * s:.1f}" for lo, la in r) + "Z"
                       for r in d["r"])

    def layer(d):
        return 2 if id(d) in hit_ids else 1 if d["o"] in okrugs else 0
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
.strike{{text-decoration:line-through;text-decoration-thickness:2px}}
.rows{{margin-top:auto;position:relative;border-top:2px solid {RULE}}}
.row{{display:grid;grid-template-columns:150px 250px minmax(0,1fr) auto;column-gap:24px;align-items:baseline;
  padding:20px 0;border-bottom:2px solid {RULE}}}
.row .lead{{font:700 40px/1 Unbounded;color:{LIME};letter-spacing:-.03em;white-space:nowrap}}
.row .rprice{{font:700 34px/1 Unbounded;color:{LIGHT};letter-spacing:-.03em;white-space:nowrap}}
.row .place{{font:600 30px/1.1 Onest;color:{LIGHT};white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.row .until{{font:500 28px Onest;color:{MUTED};white-space:nowrap}}
.stats{{margin-top:auto;position:relative;border-top:2px solid {RULE};padding-top:40px;display:grid;
  grid-template-columns:1fr 1fr;column-gap:48px}}
.stat .num{{font:800 96px/1 Unbounded;color:{LIGHT};letter-spacing:-.04em;white-space:nowrap}}
.stat .num.lime{{color:{LIME}}}
.stat .cap{{font:500 30px/1.25 Onest;color:{MUTED};margin-top:16px;text-wrap:pretty}}
.foot{{position:relative;font:500 28px Onest;color:{MUTED};margin-top:36px}}
.title{{position:relative;font:700 64px/1.12 Unbounded;color:{LIGHT};letter-spacing:-.02em;margin-top:40px;
  max-width:880px;text-wrap:balance}}
.lede{{position:relative;font:500 36px/1.3 Onest;color:{SUB};margin-top:32px;max-width:820px;text-wrap:pretty}}
.cover-illus{{position:absolute;right:70px;bottom:70px;width:300px;height:300px;object-fit:contain}}
.steps{{position:absolute;left:76px;right:76px;bottom:68px;display:flex;gap:12px}}
.steps i{{flex:1;height:12px;border-radius:6px;background:{RULE}}} .steps i.on{{background:{LIME}}}
"""

# крупная цифра не должна наезжать на карту: уменьшаем шрифт, пока не влезет в колонку
FIT_JS = """<script>for(const el of document.querySelectorAll('.headline')){let f=parseFloat(getComputedStyle(el).fontSize);
const w=el.parentElement.clientWidth;while(el.scrollWidth>w&&f>60){f-=4;el.style.fontSize=f+'px'}}</script>"""


def _page(body: str) -> str:
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{_fonts_css()}{CSS}</style></head>'
            f'<body><div class="card">{body}</div>{FIT_JS}</body></html>')


def _top(c: dict) -> str:
    return (f'<div class="top"><div class="kicker">{escape(c.get("kicker") or "")}</div>'
            f'<div class="brand">{escape(c.get("brand") or "")}</div></div>')


def _head(c: dict, margin: int | None = None) -> str:
    sub = f'<div class="headsub">{escape(c["headline_sub"])}</div>' if c.get("headline_sub") else ""
    pill = f'<div class="pill">{escape(c["deadline"])}</div>' if c.get("deadline") else ""
    style = f' style="margin-top:{margin}px"' if margin is not None else ""
    return f'<div class="head"{style}><div class="headline">{escape(c.get("headline") or "")}</div>{sub}{pill}</div>'


def _price_sub(c: dict) -> str:
    """price_sub — строка; old_price — зачёркнутая «было» перед ней (снижение цены)."""
    parts = []
    if c.get("old_price"):
        parts.append(f'было <span class="strike">{escape(c["old_price"])}</span>')
    if c.get("price_sub"):
        parts.append(escape(c["price_sub"]))
    return " · ".join(parts)


def lot_html(c: dict) -> str:
    """Один лот: kicker, brand, headline, headline_sub, deadline, price, price_sub, old_price,
    address, district, type. Подходит для «Лота дня», снижения цены, целого дома, итогов и «без борьбы»."""
    img, scale = ILLUSTRATIONS.get(c.get("type") or "parking", ILLUSTRATIONS["parking"])
    return _page(f"""
  <div class="mapbox">{map_svg(c.get("district"))}</div>
  {_top(c)}{_head(c)}
  <div class="bottom">
    <img class="illus" src="{(ASSETS / img).as_uri()}" style="transform:scale({scale})">
    <div class="info">
      <div class="price">{escape(c.get("price") or "")}</div>
      <div class="pricesub">{_price_sub(c)}</div>
      <div class="addr">{escape(c.get("address") or "")}</div>
      <div class="district">{escape(district_line(c.get("district")))}</div>
    </div>
  </div>""")


def list_html(c: dict) -> str:
    """Подборка («Неделя вперёд»): шапка как у лота, внизу 3–5 строк rows: lead, price, place, until;
    на карте подсвечены районы всех лотов (districts)."""
    rows = "".join(f'<div class="row"><div class="lead">{escape(r.get("lead") or "")}</div>'
                   f'<div class="rprice">{escape(r.get("price") or "")}</div>'
                   f'<div class="place">{escape(r.get("place") or "")}</div>'
                   f'<div class="until">{escape(r.get("until") or "")}</div></div>' for r in c.get("rows", [])[:5])
    return _page(f"""
  <div class="mapbox" style="top:96px">{map_svg(c.get("districts") or [])}</div>
  {_top(c)}{_head(c, 70)}
  <div class="rows">{rows}</div>""")


def stats_html(c: dict) -> str:
    """Сводка («Итоги недели»): крупное число в шапке, внизу два показателя stats: [{num, cap, lime}], строка foot."""
    stats = "".join(f'<div class="stat"><div class="num{" lime" if st.get("lime") else ""}">{escape(st["num"])}</div>'
                    f'<div class="cap">{escape(st.get("cap") or "")}</div></div>' for st in c.get("stats", [])[:2])
    foot = f'<div class="foot">{escape(c["foot"])}</div>' if c.get("foot") else ""
    return _page(f"""
  <div class="mapbox">{map_svg(c.get("districts") or [])}</div>
  {_top(c)}{_head(c)}
  <div class="stats">{stats}</div>{foot}""")


def cover_html(c: dict) -> str:
    """Обложка без данных («Ликбез», «О канале»): headline (номер шага), title, lede, иллюстрация type,
    step = (номер, всего) — шкала серии внизу."""
    img, scale = ILLUSTRATIONS.get(c.get("type") or "", (None, 1))
    illus = f'<img class="cover-illus" src="{(ASSETS / img).as_uri()}" style="transform:scale({scale})">' if img else ""
    if c.get("step"):   # шкала серии: step = (номер, всего)
        n, total = c["step"]
        illus += '<div class="steps">' + "".join(f'<i class="{"on" if i < n else ""}"></i>' for i in range(total)) + "</div>"
    head = f'<div class="head" style="margin-top:90px"><div class="headline">{escape(c["headline"])}</div></div>' \
        if c.get("headline") else ""
    lede = f'<div class="lede">{escape(c["lede"])}</div>' if c.get("lede") else ""
    return _page(f"""{illus}{_top(c)}{head}
  <div class="title">{escape(c.get("title") or "")}</div>{lede}""")


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
