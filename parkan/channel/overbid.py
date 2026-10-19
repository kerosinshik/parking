"""Видео для поста в Instagram (лента, 4:5): «ставка, которая не стала покупкой».

Сюжет — аномальная ставка на торгах и что было дальше: ставка → фото места → во сколько раз выше старта →
сумма прописью из протокола → лот снова на торгах → за сколько продали → почему так бывает → призыв в бот.
Кадры 1080×1350 (формат поста 4:5, без зон интерфейса Reels), стиль и шрифты — как у карточек канала.
Монтаж — вне проекта: кадры кладутся на таймлайн по полю frames[i]["sec"].

Факты — JSON (пример — docs/design/feed/2026-10-18-festivalnaya/facts.json):
  address        адрес дома
  district       район (как в moscow-geo.json)
  place          номер места, «м/м 56»
  area           площадь, м²
  level          где место, «подземный этаж −1» (необязательно)
  start          стартовая цена первых торгов, ₽
  bid            ставка победителя по протоколу, ₽
  bid_words      та же сумма прописью — как в протоколе
  bid_date       дата первых торгов, «21 ноября 2025»
  resale_date    дата повторных торгов, «5 марта 2026»
  resale_start   старт повторных торгов, ₽ (по умолчанию = start)
  resale_price   цена продажи на повторных торгах, ₽
  months         сколько месяцев прошло между торгами, «3,5»
  photo          фото места (путь относительно файла фактов; необязательно)
  word, campaign, bot — как у роликов (reels.py)
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

from . import html_cards as hc
from .html_cards import ASSETS, BG, ILLUSTRATIONS, LIGHT, LIME, MUTED, RULE, SUB
from .reels import SEARCH_ICON, bot_link, register  # noqa: F401 — метки и ссылки общие с роликами

PW, PH = 1080, 1350
PAD = 80

CSS = f"""
*{{box-sizing:border-box}} html,body{{margin:0;background:{BG}}}
.post{{width:{PW}px;height:{PH}px;background:{BG};position:relative;overflow:hidden;font-family:Onest,sans-serif;
  padding:104px {PAD}px {PAD}px;display:flex;flex-direction:column}}
.steps{{position:absolute;left:{PAD}px;right:{PAD}px;top:44px;display:flex;gap:10px}}
.steps i{{flex:1;height:8px;border-radius:4px;background:{RULE}}} .steps i.on{{background:{LIME}}}
.top{{display:flex;justify-content:space-between;align-items:center}}
.kicker{{background:{LIME};color:{BG};font:700 30px Onest;letter-spacing:.04em;padding:12px 26px;border-radius:40px}}
.brand{{font:700 28px Unbounded;color:{LIGHT};letter-spacing:.06em}}
.lead{{font:600 54px/1.15 Onest;color:{SUB};margin-top:84px;text-wrap:balance}}
.big{{font:800 260px/0.95 Unbounded;color:{LIME};letter-spacing:-.05em;white-space:nowrap;margin-top:30px}}
.unit{{font:700 72px/1 Unbounded;color:{LIGHT};letter-spacing:-.02em;margin-top:14px}}
.exact{{margin-top:auto;border-top:2px solid {RULE};padding-top:36px;font:700 64px/1 Unbounded;color:{LIGHT};
  letter-spacing:-.03em;white-space:nowrap}}
.note{{font:500 38px/1.3 Onest;color:{MUTED};margin-top:18px;text-wrap:pretty}}
.photo{{margin-top:36px;height:760px;border-radius:32px;overflow:hidden;flex:none}}
.photo img{{width:100%;height:100%;object-fit:cover;display:block}}
.addr{{font:700 50px/1.15 Onest;color:{LIGHT};margin-top:36px}}
.district{{font:500 36px Onest;color:{MUTED};margin-top:10px}}
.facts{{display:flex;gap:16px;margin-top:26px;flex-wrap:wrap}}
.chip{{border:2px solid {LIME};color:{LIME};font:600 34px Onest;padding:10px 24px;border-radius:40px}}
.scale{{display:flex;gap:56px;align-items:flex-end;margin-top:64px}}
.scale .box{{background:{LIME};border-radius:6px}}
.scale .cap{{font:500 34px/1.25 Onest;color:{MUTED};margin-top:20px}}
.scale .cap b{{display:block;font:700 46px Unbounded;color:{LIGHT};letter-spacing:-.02em;margin-top:6px}}
.times{{margin-top:auto;font:800 220px/0.9 Unbounded;color:{LIME};letter-spacing:-.05em}}
.times small{{display:block;font:500 40px/1.3 Onest;color:{SUB};letter-spacing:0;margin-top:20px}}
.title{{font:700 76px/1.1 Unbounded;color:{LIGHT};letter-spacing:-.02em;margin-top:84px;text-wrap:balance}}
.title em{{font-style:normal;color:{LIME}}}
.quote{{margin-top:56px;border-left:10px solid {LIME};padding:8px 0 8px 44px;font:700 62px/1.2 Onest;color:{LIGHT};
  text-wrap:balance}}
.quote:first-letter{{text-transform:uppercase}}
.src{{margin-top:auto;font:500 34px/1.3 Onest;color:{MUTED}}}
.tl{{margin-top:auto;display:flex;flex-direction:column;gap:0}}
.ev{{display:grid;grid-template-columns:56px 1fr;column-gap:36px}}
.ev .dot{{width:56px;height:56px;border-radius:50%;border:6px solid {LIME};background:{BG};position:relative;z-index:1}}
.ev .dot.on{{background:{LIME}}}
.ev .rail{{width:6px;background:{RULE};margin:0 auto;height:100%}}
.ev .when{{font:600 36px Onest;color:{MUTED};margin-top:6px}}
.ev .what{{font:700 52px/1.15 Onest;color:{LIGHT};margin:10px 0 56px;text-wrap:balance}}
.ev .what s{{color:{MUTED};text-decoration-thickness:5px}}
.checks{{margin-top:auto;display:flex;flex-direction:column;gap:30px}}
.check{{display:flex;gap:28px;align-items:center;font:600 44px/1.2 Onest;color:{SUB}}}
.check b{{flex:none;width:62px;height:62px;border-radius:50%;border:3px solid {LIME};color:{LIME};
  display:flex;align-items:center;justify-content:center;font:700 32px Unbounded}}
.cta-illus{{display:block;width:400px;height:400px;object-fit:contain;margin:0 auto auto}}
.search{{background:{LIGHT};border-radius:64px;padding:30px 40px;display:flex;align-items:center;gap:28px}}
.search span{{font:700 56px Onest;color:{BG}}}
.search i{{display:inline-block;width:5px;height:58px;background:{BG};margin-left:6px;vertical-align:-10px}}
.hint{{font:500 38px/1.3 Onest;color:{MUTED};margin-top:28px;text-align:center}}
.hint .word{{display:block;margin-top:10px;font:800 64px Unbounded;color:{LIME};letter-spacing:.02em}}
"""


def money(rub: float) -> tuple[str, str]:
    """2 128 887 000 → ("2,13", "млрд ₽"); 3 483 000 → ("3,48", "млн ₽"); 850 000 → ("850", "тыс. ₽")."""
    for div, unit in ((1e9, "млрд ₽"), (1e6, "млн ₽")):
        if rub >= div:
            v = rub / div
            s = f"{v:.2f}".rstrip("0").rstrip(".") if v < 10 else f"{v:.1f}".rstrip("0").rstrip(".")
            return s.replace(".", ","), unit
    return f"{round(rub / 1000):,}".replace(",", " "), "тыс. ₽"


def money_text(rub: float) -> str:
    return " ".join(money(rub))


def exact(rub: float) -> str:
    """2128887000 → «2 128 887 000 ₽» (узкие неразрывные пробелы между разрядами)."""
    return f"{round(rub):,}".replace(",", " ") + " ₽"


def times(a: float, b: float) -> str:
    """Во сколько раз a больше b: 1650, 611, 2,7."""
    r = a / b
    return f"{r:,.0f}".replace(",", " ") if r >= 10 else f"{r:.1f}".replace(".", ",")


def _page(body: str, step: int, total: int) -> str:
    steps = "".join(f'<i class="{"on" if i < step else ""}"></i>' for i in range(total))
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{hc._fonts_css()}{CSS}</style></head>'
            f'<body><div class="post"><div class="steps">{steps}</div>{body}</div></body></html>')


def _top(kicker: str) -> str:
    return f'<div class="top"><div class="kicker">{escape(kicker)}</div><div class="brand">ПОПУТНЫЙ МЕТР</div></div>'


def frame_bid(f: dict) -> str:
    num, unit = money(f["bid"])
    return f"""{_top("ТОРГИ МОСКВЫ")}
  <div class="lead">Столько предложили на городских торгах за одно машино-место</div>
  <div class="big">{escape(num)}</div><div class="unit">{escape(unit)}</div>
  <div class="exact">{exact(f["bid"])}</div>
  <div class="note">ставка победителя по протоколу аукциона</div>"""


def frame_place(f: dict) -> str:
    chips = [f.get("place"), f'{f["area"]:g} м²'.replace(".", ",") if f.get("area") else None, f.get("level")]
    chips = "".join(f'<div class="chip">{escape(c)}</div>' for c in chips if c)
    photo = (f'<div class="photo"><img src="{Path(f["photo"]).resolve().as_uri()}"></div>'
             if f.get("photo") else "")
    return f"""{_top("ВОТ ЭТО МЕСТО")}
  {photo}
  <div class="addr">{escape(f["address"])}</div>
  <div class="district">{escape(hc.district_line(f.get("district")))}</div>
  <div class="facts">{chips}</div>"""


def frame_ratio(f: dict) -> str:
    big = 560
    small = max(big / (f["bid"] / f["start"]) ** 0.5, 6)   # площади квадратов — как цены
    return f"""{_top("СРАВНИМ")}
  <div class="scale">
    <div><div class="box" style="width:{small:.0f}px;height:{small:.0f}px"></div>
      <div class="cap">старт<b>{escape(money_text(f["start"]))}</b></div></div>
    <div><div class="box" style="width:{big}px;height:{big}px"></div>
      <div class="cap">ставка<b>{escape(money_text(f["bid"]))}</b></div></div>
  </div>
  <div class="times">×{times(f["bid"], f["start"])}<small>ставка выше стартовой цены</small></div>"""


def frame_words(f: dict) -> str:
    return f"""{_top("ПРОТОКОЛ")}
  <div class="title">Это не опечатка: в протоколе сумма <em>прописью</em></div>
  <div class="quote">«{escape(f["bid_words"])}»</div>
  <div class="src">Протокол аукциона, {escape(f["bid_date"])}</div>"""


def frame_relist(f: dict) -> str:
    start = f.get("resale_start") or f["start"]
    return f"""{_top("ЧТО БЫЛО ДАЛЬШЕ")}
  <div class="title">Через {escape(f["months"])} месяца место <em>снова</em> на торгах</div>
  <div class="tl">
    <div class="ev"><div><div class="dot on"></div><div class="rail"></div></div>
      <div><div class="when">{escape(f["bid_date"])}</div><div class="what">Ставка {escape(money_text(f["bid"]))}</div></div></div>
    <div class="ev"><div><div class="dot"></div></div>
      <div><div class="when">{escape(f["resale_date"])}</div>
        <div class="what">Новые торги, старт снова {escape(money_text(start))}</div></div></div>
  </div>"""


def frame_result(f: dict) -> str:
    num, unit = money(f["resale_price"])
    start = f.get("resale_start") or f["start"]
    return f"""{_top("ИТОГ")}
  <div class="lead">На повторных торгах место продали за</div>
  <div class="big">{escape(num)}</div><div class="unit">{escape(unit)}</div>
  <div class="exact" style="font-size:52px">в {times(f["bid"], f["resale_price"])} раз меньше той ставки</div>
  <div class="note">и в {times(f["resale_price"], start)} раза выше стартовой цены</div>"""


def frame_why(f: dict) -> str:
    items = ["Победитель должен заключить договор и оплатить цену",
             "Если не заключает — задаток остаётся у города",
             "Лот выставляют на торги заново"]
    checks = "".join(f'<div class="check"><b>{i + 1}</b>{escape(t)}</div>' for i, t in enumerate(items))
    return f"""{_top("КАК ТАК?")}
  <div class="title">Ставка на аукционе — <em>ещё не покупка</em></div>
  <div class="checks">{checks}</div>"""


def frame_cta(f: dict) -> str:
    bot = f.get("bot") or "Попутный метр"
    word = (f'<br>и отправь боту слово<b class="word">{escape(f["word"].upper())}</b>') if f.get("word") else ""
    img, scale = ILLUSTRATIONS["parking"]
    return f"""{_top("В TELEGRAM")}
  <div class="title">Лоты торгов, итоги аукционов и такие истории — <em>в Telegram</em></div>
  <img class="cta-illus" src="{(ASSETS / img).as_uri()}" style="transform:scale({scale * 0.8})">
  <div class="search">{SEARCH_ICON}<span>{escape(bot)}<i></i></span></div>
  <div class="hint">Ищи «{escape(bot)}» в Telegram{word}</div>"""


def card(f: dict) -> str:
    """Карточка поста в Telegram: кадр со ставкой в формате карточек канала (1080×1080, без полосы шагов)."""
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{hc._fonts_css()}{CSS}</style></head>'
            f'<body><div class="post" style="height:1080px;padding:68px 76px">{frame_bid(f)}</div></body></html>')


# сценарий: (имя файла, секунды на таймлайне, кадр)
SCRIPT = (("01-bid", "0–4", frame_bid), ("02-place", "4–9", frame_place), ("03-ratio", "9–14", frame_ratio),
          ("04-words", "14–20", frame_words), ("05-relist", "20–25", frame_relist), ("06-result", "25–30", frame_result),
          ("07-why", "30–36", frame_why), ("08-cta", "36–40", frame_cta))


def frames(facts: dict) -> list[dict]:
    """HTML кадров по сценарию: [{name, sec, html}]."""
    return [{"name": n, "sec": sec, "html": _page(make(facts), i + 1, len(SCRIPT))}
            for i, (n, sec, make) in enumerate(SCRIPT)]


def render(facts: dict, out_dir) -> list[Path]:
    """Нарисовать кадры в out_dir: 01-bid.png … 08-cta.png."""
    out = Path(out_dir)
    return [hc.render_html(fr["html"], out / f'{fr["name"]}.png', size=(PW, PH)) for fr in frames(facts)]


def load_facts(path) -> dict:
    p = Path(path)
    f = json.loads(p.read_text(encoding="utf-8"))
    need = ("address", "start", "bid", "bid_words", "bid_date", "resale_date", "resale_price", "months")
    missing = [k for k in need if not f.get(k)]
    if missing:
        raise ValueError("в фактах видео нет: " + ", ".join(missing))
    if f.get("photo"):
        f["photo"] = str((p.parent / f["photo"]).resolve())
    return f
