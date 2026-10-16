"""Кадры роликов для Instagram Reels: вертикальные 1080×1920 в стиле карточек канала.

Ролик собирается из 5 кадров по покадровому сценарию (docs/channel.md, «Ролики»): цена → адрес и карта →
сравнение с медианой района → оговорка про стартовую цену → призыв в бот. Кадры — HTML с теми же
токенами, шрифтами и картой районов, что у html_cards; PNG рисует тот же headless Chromium.
Монтаж (длительности, анимация, звук) — вне проекта: кадры кладутся на таймлайн по полю frames[i]["sec"].

Факты ролика — JSON (пример — docs/design/reels/2026-10-15-mozhayskoe/facts.json):
  type        parking | flat (иллюстрация и слово «машино-место»/«квартира»)
  price       стартовая цена, ₽
  median      медиана старта в районе, ₽ (для квартир — посчитайте по ₽/м² заранее и дайте цену)
  address     адрес объекта
  district    район (как в moscow-geo.json, «Кузьминки», «Мещанский район»)
  area        площадь, м² (необязательно)
  scope       «района» или «округа» — с чем сравниваем (как в «Лоте дня»; по умолчанию «района»)
  until       срок заявок для плашки на кадре с адресом, «заявки до 3 ноября» (необязательно)
  url         ссылка на лот (для текстов, на кадрах не печатается)
  lot_id      id лота в снимке бота — его откроют метка и кодовое слово
  campaign    метка ролика для аналитики: ig01 → t.me/<бот>?start=ig01 (буквы, цифры, _ и -, до 64 знаков)
  word        кодовое слово для тех, кто найдёт бота поиском: печатается на последнем кадре
  bot         имя канала/бота для поиска в Telegram (по умолчанию «Попутный метр»)

Второй сценарий — «топ» (format: "top", пример — docs/design/reels/2026-10-16-top/facts.json): самое дешёвое
→ три самых дешёвых лота → медианы округов (самый дешёвый и самый дорогой) → оговорка → призыв в бот.
Факты собирает top_facts() из журнала (`channel-reel --top`):
  lots        [{price, address, district, area, until, lot_id, url}] — по цене, по адресу один лот
  adm         [{name, short, med, houses, n}] — самый дешёвый и самый дорогой округ по медиане старта (по домам)
  lot_id      лот № 1 — его откроют метка и кодовое слово
"""

from __future__ import annotations

import json
import os
import re
from html import escape
from pathlib import Path

from . import html_cards as hc
from .html_cards import ASSETS, BG, ILLUSTRATIONS, LIGHT, LIME, MUTED, RULE, SUB
from .templates import day

RW, RH = 1080, 1920
# безопасная зона Reels: сверху ник и звук, снизу подпись и кнопки — важное держим между ними
SAFE_TOP, SAFE_BOTTOM = 220, 420

DEFAULT_BOT = os.environ.get("PARKAN_BOT_USERNAME", "poputny_metr_bot")
KIND = {"parking": "машино-место", "flat": "квартира"}

CSS = f"""
*{{box-sizing:border-box}} html,body{{margin:0;background:{BG}}}
.reel{{width:{RW}px;height:{RH}px;background:{BG};position:relative;overflow:hidden;font-family:Onest,sans-serif;
  padding:{SAFE_TOP}px 84px {SAFE_BOTTOM}px;display:flex;flex-direction:column}}
.steps{{position:absolute;left:84px;right:84px;top:120px;display:flex;gap:12px}}
.steps i{{flex:1;height:10px;border-radius:5px;background:{RULE}}} .steps i.on{{background:{LIME}}}
.top{{display:flex;justify-content:space-between;align-items:center}}
.kicker{{background:{LIME};color:{BG};font:700 34px Onest;letter-spacing:.04em;padding:14px 30px;border-radius:44px}}
.brand{{font:700 30px Unbounded;color:{LIGHT};letter-spacing:.06em}}
.lead{{font:600 58px/1.15 Onest;color:{SUB};margin-top:120px;text-wrap:balance}}
.big{{font:800 250px/0.95 Unbounded;color:{LIME};letter-spacing:-.05em;white-space:nowrap;margin-top:28px}}
.unit{{font:700 64px/1 Unbounded;color:{LIGHT};letter-spacing:-.02em;margin-top:18px}}
.note{{font:500 40px/1.25 Onest;color:{MUTED};margin-top:28px}}
.illus{{position:absolute;right:60px;bottom:{SAFE_BOTTOM + 20}px;width:560px;height:560px;object-fit:contain}}
.mapbox{{margin:40px auto 0;width:{hc.W}px;height:{hc.H}px;transform:scale(1.55);transform-origin:top center}}
.map{{display:block;-webkit-mask-image:radial-gradient(closest-side,#000 62%,transparent 100%);
  mask-image:radial-gradient(closest-side,#000 62%,transparent 100%)}}
.panel{{margin-top:auto;border-top:2px solid {RULE};padding-top:44px}}
.addr{{font:700 56px/1.15 Onest;color:{LIGHT};text-wrap:balance}}
.district{{font:500 40px Onest;color:{MUTED};margin-top:16px}}
.facts{{display:flex;gap:20px;margin-top:36px;flex-wrap:wrap}}
.chip{{border:2px solid {LIME};color:{LIME};font:600 40px Onest;padding:14px 28px;border-radius:48px}}
.bars{{margin-top:120px;display:flex;flex-direction:column;gap:56px}}
.bar .cap{{font:500 44px Onest;color:{MUTED};margin-bottom:18px}}
.bar .line{{display:flex;align-items:center;gap:28px}}
.bar .fill{{height:150px;border-radius:24px}}
.bar .val{{font:700 68px/1 Unbounded;color:{LIGHT};letter-spacing:-.03em;white-space:nowrap}}
.diff{{margin-top:auto}}
.diff .big{{font-size:240px}}
.diff .note{{font-size:48px;color:{SUB}}}
.title{{font:700 84px/1.1 Unbounded;color:{LIGHT};letter-spacing:-.02em;margin-top:110px;text-wrap:balance}}
.title em{{font-style:normal;color:{LIME}}}
.checks{{margin-top:auto;display:flex;flex-direction:column;gap:30px}}
.check{{display:flex;gap:28px;align-items:center;font:600 46px/1.2 Onest;color:{SUB}}}
.check b{{flex:none;width:64px;height:64px;border-radius:50%;border:3px solid {LIME};color:{LIME};
  display:flex;align-items:center;justify-content:center;font:700 34px Unbounded}}
.cta-illus{{display:block;width:460px;height:460px;object-fit:contain;margin:10px auto auto}}
.search{{background:{LIGHT};border-radius:70px;padding:34px 44px;display:flex;align-items:center;gap:30px}}
.search svg{{flex:none}}
.search span{{font:700 58px Onest;color:{BG}}}
.search i{{display:inline-block;width:5px;height:62px;background:{BG};margin-left:6px;vertical-align:-10px}}
.hint{{font:500 40px/1.3 Onest;color:{MUTED};margin-top:30px;text-align:center}}
.rows{{margin-top:56px;display:flex;flex-direction:column;gap:44px}}
.row{{display:flex;gap:32px;align-items:flex-start;border-top:2px solid {RULE};padding-top:40px}}
.row b{{flex:none;width:76px;height:76px;border-radius:50%;background:{LIME};color:{BG};
  display:flex;align-items:center;justify-content:center;font:800 40px Unbounded;margin-top:6px}}
.row .price{{font:800 84px/1 Unbounded;color:{LIME};letter-spacing:-.03em;white-space:nowrap}}
.row .addr{{font-size:44px;margin-top:16px}}
.row .district{{font-size:34px;margin-top:10px}}
.nw{{white-space:nowrap}}
.title.small{{font-size:68px;margin-top:90px}}
.hint .word{{display:block;margin-top:10px;font:800 64px Unbounded;color:{LIME};letter-spacing:.02em}}
"""


def money(rub: float) -> tuple[str, str]:
    """1 290 000 → ("1,29", "млн ₽"); 850 000 → ("850", "тыс. ₽")."""
    if rub >= 1_000_000:
        v = rub / 1_000_000
        s = f"{v:.2f}".rstrip("0").rstrip(".") if v < 10 else f"{v:.1f}".rstrip("0").rstrip(".")
        return s.replace(".", ","), "млн ₽"
    return f"{round(rub / 1000):,}".replace(",", " "), "тыс. ₽"


def money_text(rub: float) -> str:
    return " ".join(money(rub))


def discount(price: float, median: float) -> int:
    """На сколько процентов старт ниже медианы (округление до целого)."""
    return round((1 - price / median) * 100)


def _page(body: str, step: int, total: int = 5) -> str:
    steps = "".join(f'<i class="{"on" if i < step else ""}"></i>' for i in range(total))
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{hc._fonts_css()}{CSS}</style></head>'
            f'<body><div class="reel"><div class="steps">{steps}</div>{body}</div></body></html>')


def _top(kicker: str) -> str:
    return f'<div class="top"><div class="kicker">{escape(kicker)}</div><div class="brand">ПОПУТНЫЙ МЕТР</div></div>'


def frame_price(f: dict) -> str:
    num, unit = money(f["price"])
    img, scale = ILLUSTRATIONS.get(f.get("type") or "parking", ILLUSTRATIONS["parking"])
    return _page(f"""{_top("ТОРГИ МОСКВЫ")}
  <div class="lead">В Москве на торгах появился объект за</div>
  <div class="big">{escape(num)}</div><div class="unit">{escape(unit)}</div>
  <div class="note">{escape(KIND.get(f.get("type"), "объект"))} · стартовая цена</div>
  <img class="illus" src="{(ASSETS / img).as_uri()}" style="transform:scale({scale})">""", 1)


def frame_place(f: dict) -> str:
    chips = []
    if f.get("area"):
        chips.append(f'{f["area"]:g} м²'.replace(".", ","))
    chips.append(money_text(f["price"]))
    if f.get("until"):
        chips.append(f["until"])
    chips = "".join(f'<div class="chip">{escape(c)}</div>' for c in chips)
    return _page(f"""{_top("ГДЕ ЭТО")}
  <div style="position:relative">{_map(f.get("district"))}</div>
  <div class="panel">
    <div class="addr">{escape(f["address"])}</div>
    <div class="district">{escape(hc.district_line(f.get("district")))}</div>
    <div class="facts">{chips}</div>
  </div>""", 2)


def _map(district: str | None) -> str:
    return f'<div class="mapbox">{hc.map_svg(district)}</div>'


def frame_compare(f: dict) -> str:
    price, median = f["price"], f["median"]
    pct = discount(price, median)
    full = 912 - 330            # ширина полосы медианы; подпись справа
    return _page(f"""{_top("СРАВНИМ")}
  <div class="bars">
    <div class="bar"><div class="cap">Медиана {escape(f.get("scope") or "района")}</div>
      <div class="line"><div class="fill" style="width:{full}px;background:{RULE}"></div>
      <div class="val">{escape(money_text(median))}</div></div></div>
    <div class="bar"><div class="cap">Этот объект</div>
      <div class="line"><div class="fill" style="width:{full * price / median:.0f}px;background:{LIME}"></div>
      <div class="val">{escape(money_text(price))}</div></div></div>
  </div>
  <div class="diff"><div class="big">−{pct}%</div><div class="note">ниже медианы {escape(f.get("scope") or "района")}</div></div>""", 3)


def frame_caveat(f: dict) -> str:
    top = f.get("format") == "top"
    items = ["Стартовые цены растут на аукционе" if top else "Стартовая цена растёт на аукционе",
             "Обременения и состояние", "Задаток и сроки заявок"]
    checks = "".join(f'<div class="check"><b>{i + 1}</b>{escape(t)}</div>' for i, t in enumerate(items))
    title, note = (("Это <em>стартовые</em> цены, а не цены покупки", "Условия каждого лота нужно проверять") if top else
                   ("Это <em>стартовая</em> цена, а не гарантированная цена покупки", "Условия объекта нужно проверять"))
    return _page(f"""{_top("ВАЖНО")}
  <div class="title">{title}</div>
  <div class="note" style="font-size:46px;color:{SUB};margin-top:44px">{note}</div>
  <div class="checks">{checks}</div>""", 4)


SEARCH_ICON = (f'<svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="{BG}" stroke-width="2.6" '
               'stroke-linecap="round"><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5 21 21"/></svg>')


def frame_cta(f: dict) -> str:
    bot = f.get("bot") or "Попутный метр"
    img, scale = ILLUSTRATIONS.get(f.get("type") or "parking", ILLUSTRATIONS["parking"])
    word = (f'<br>и отправь боту слово<b class="word">{escape(f["word"].upper())}</b>') if f.get("word") else ""
    return _page(f"""{_top("В TELEGRAM")}
  <div class="title">{"Лот № 1 и ссылка на торги" if f.get("format") == "top" else "Весь объект и ссылка на торги"} — <em>в боте</em></div>
  <img class="cta-illus" src="{(ASSETS / img).as_uri()}" style="transform:scale({scale * 0.8})">
  <div class="search">{SEARCH_ICON}<span>{escape(bot)}<i></i></span></div>
  <div class="hint">Ищи «{escape(bot)}» в Telegram{word}</div>""", 5)


ADM_SHORT = {"Центральный": "ЦАО", "Северный": "САО", "Северо-Восточный": "СВАО", "Восточный": "ВАО",
             "Юго-Восточный": "ЮВАО", "Южный": "ЮАО", "Юго-Западный": "ЮЗАО", "Западный": "ЗАО",
             "Северо-Западный": "СЗАО", "Зеленоградский": "ЗелАО", "Новомосковский": "НАО", "Троицкий": "ТАО"}


def adm_short(name: str | None) -> str:
    """«Юго-Западный административный округ» → «ЮЗАО»."""
    head = (name or "").split(" административный")[0].strip()
    return ADM_SHORT.get(head, head)


def frame_cheapest(f: dict) -> str:
    first = f["lots"][0]
    num, unit = money(first["price"])
    img, scale = ILLUSTRATIONS.get(f.get("type") or "parking", ILLUSTRATIONS["parking"])
    return _page(f"""{_top("ТОРГИ МОСКВЫ")}
  <div class="lead">Самое дешёвое <span class="nw">{escape(KIND.get(f.get("type"), "объект"))}</span> на торгах Москвы —</div>
  <div class="big">{escape(num)}</div><div class="unit">{escape(unit)}</div>
  <div class="note">из лотов, на которые ещё можно подать заявку</div>
  <img class="illus" src="{(ASSETS / img).as_uri()}" style="transform:scale({scale})">""", 1)


def frame_top(f: dict) -> str:
    rows = []
    for i, lot in enumerate(f["lots"], 1):
        meta = " · ".join(x for x in (hc.district_line(lot.get("district")), lot.get("until")) if x)
        rows.append(f'<div class="row"><b>{i}</b><div><div class="price">{escape(money_text(lot["price"]))}</div>'
                    f'<div class="addr">{escape(lot["address"])}</div><div class="district">{escape(meta)}</div></div></div>')
    kind = "машино-места" if (f.get("type") or "parking") == "parking" else "квартиры"
    return _page(f"""{_top(f"ТОП-{len(rows)}")}
  <div class="title small">Самые дешёвые <span class="nw">{kind}</span> <em>сейчас</em></div>
  <div class="rows">{"".join(rows)}</div>""", 2)


def frame_adm(f: dict) -> str:
    lo, hi = f["adm"][0], f["adm"][-1]
    full = 912 - 330
    ratio = f"{hi['med'] / lo['med']:.1f}".replace(".", ",")
    bars = "".join(
        f'<div class="bar"><div class="cap">{escape(cap)} — {escape(a.get("short") or adm_short(a["name"]))}</div>'
        f'<div class="line"><div class="fill" style="width:{full * a["med"] / hi["med"]:.0f}px;background:{color}"></div>'
        f'<div class="val">{escape(money_text(a["med"]))}</div></div></div>'
        for cap, a, color in (("Дешевле всех", lo, LIME), ("Дороже всех", hi, RULE)))
    return _page(f"""{_top("ПО ОКРУГАМ")}
  <div class="lead" style="margin-top:90px">Медиана стартовой цены по округам</div>
  <div class="bars" style="margin-top:70px">{bars}</div>
  <div class="diff"><div class="big">×{ratio}</div><div class="note">разница между округами</div></div>""", 3)


# сценарий: (имя файла, секунды на таймлайне, кадр)
SCRIPT = (("01-price", "0–3", frame_price), ("02-place", "3–7", frame_place), ("03-compare", "7–12", frame_compare),
          ("04-caveat", "12–17", frame_caveat), ("05-cta", "17–20", frame_cta))


TOP_SCRIPT = (("01-cheapest", "0–3", frame_cheapest), ("02-top", "3–9", frame_top), ("03-adm", "9–13", frame_adm),
              ("04-caveat", "13–17", frame_caveat), ("05-cta", "17–20", frame_cta))


def frames(facts: dict) -> list[dict]:
    """HTML кадров по сценарию: [{name, sec, html}]."""
    script = TOP_SCRIPT if facts.get("format") == "top" else SCRIPT
    return [{"name": n, "sec": sec, "html": make(facts)} for n, sec, make in script]


def top_facts(con, today, kind: str = "parking", n: int = 3, days: int = 10, min_houses: int = 7) -> dict:
    """Факты ролика-топа из журнала (после lots.load): n самых дешёвых лотов с заявками ещё хотя бы days дней
    и самый дешёвый/дорогой округ по медиане старта (округа, где лоты хотя бы в min_houses домах)."""
    from datetime import timedelta
    from . import lots
    until_from = today + timedelta(days=days)
    found = lots.cheapest(con, kind, until_from, n)
    adms = lots.adm_medians(con, kind, min_houses)
    if not found or len(adms) < 2:
        raise ValueError("в журнале не хватает лотов или округов для топа")
    total = con.execute("SELECT count(*) FROM ch_sale WHERE kind = ?", [kind]).fetchone()[0]
    return {
        "format": "top", "type": kind, "date": today.isoformat(),
        "lots": [{"price": r["price"], "address": r["place"], "district": r["district"], "area": r["space"],
                  "until": f"заявки до {day(r['end_d'])}", "trades": r["trades_d"].isoformat() if r["trades_d"] else None,
                  "lot_id": r["lot_id"], "url": r["url"]} for r in found],
        "adm": [{"name": a["adm"], "short": adm_short(a["adm"]), "med": a["med"], "houses": a["houses"], "n": a["n"]}
                for a in (adms[0], adms[-1])],
        "lot_id": found[0]["lot_id"],
        "source": (f"журнал Пульса (набор № 1461) на {today:%d.%m.%Y}: {total} лотов {KIND.get(kind, kind)} в продаже; "
                   f"топ — лоты с заявками до {until_from:%d.%m.%Y} и позже, по адресу один; медианы округов — по домам, "
                   f"только округа, где лоты хотя бы в {min_houses} домах ({len(adms)} округов)"),
    }


def render(facts: dict, out_dir) -> list[Path]:
    """Нарисовать кадры ролика в out_dir: 01-price.png … 05-cta.png."""
    out = Path(out_dir)
    return [hc.render_html(fr["html"], out / f'{fr["name"]}.png', size=(RW, RH)) for fr in frames(facts)]


def load_facts(path) -> dict:
    f = json.loads(Path(path).read_text(encoding="utf-8"))
    need = ("lots", "adm") if f.get("format") == "top" else ("price", "median", "address")
    missing = [k for k in need if not f.get(k)]
    if missing:
        raise ValueError("в фактах ролика нет: " + ", ".join(missing))
    if f.get("campaign") and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", f["campaign"]):
        raise ValueError("метка ролика — латиница, цифры, _ и -, до 64 знаков (так требует Telegram)")
    return f


def bot_link(campaign: str, bot_username: str = DEFAULT_BOT) -> str:
    return f"https://t.me/{bot_username}?start={campaign}"


def register(facts: dict, path) -> dict:
    """Записать метку ролика в campaigns.json бота (рядом со снимком lots.json): метка → лот и кодовое слово."""
    p = Path(path)
    items = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    word = facts.get("word")
    clash = [t for t, c in items.items() if t != facts["campaign"] and word and (c.get("word") or "").lower() == word.lower()]
    if clash:
        raise ValueError(f"кодовое слово «{word}» уже занято меткой {clash[0]}")
    items[facts["campaign"]] = {k: v for k, v in (("lot", facts.get("lot_id")), ("word", word),
                                                   ("title", facts.get("title") or facts.get("address")),
                                                   ("date", facts.get("date"))) if v}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return items[facts["campaign"]]
