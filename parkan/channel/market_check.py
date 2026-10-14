"""«Проверить рынок»: автор вручную подбирает объявления рядом с лотом, бот считает и рисует карточку.

Автоматически собирать объявления ЦИАН, Авито и других площадок нельзя (их правила запрещают роботов и базы
из объявлений), поэтому сравнение с рынком делает человек:

1. Под черновиком лота в продаже со стартом ниже медианы (квартиры и машино-места) — кнопки-ссылки на поиск объявлений рядом (search_links). Telegram открывает их
   сам, сервер не нужен. Отправленные черновики запоминаются в channel_state/kitchen.json (id сообщения → лот).
2. Автор смотрит объявления и отвечает (reply) на черновик в кухне, по строке на объявление:
   «https://www.cian.ru/sale/flat/123/ 14,5 млн 42» — ссылка, цена, площадь (у машино-мест хватит ссылки и цены).
3. `parkan channel-market` забирает ответы через getUpdates (смещение — channel_state/updates.json), разбирает строки
   (parse_listings), считает ₽/м² и складывает в папку дня черновик рубрики «Рядом на рынке»: факты, карточку,
   шаблонный текст — дальше как обычно: текст по навыку channel-copy, channel-lint, channel-send.
   Нераспознанные строки бот называет ответом в кухне.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlparse

from . import templates as t

KITCHEN = "kitchen.json"
UPDATES = "updates.json"
KEEP_DAYS = 30
M2_RANGE = (50_000, 3_000_000)     # правдоподобная цена ₽/м² в объявлении: защита от опечаток
SITES = {"cian.ru": "ЦИАН", "avito.ru": "Авито", "realty.yandex.ru": "Яндекс Недвижимость", "domclick.ru": "Домклик",
         "m2.ru": "М2"}


# ---------------------------------------------------------------- 1. кнопки под черновиком

def _street(place: str | None) -> str:
    return re.split(r",\s*(дом|владение|строение|корпус)\b", place or "")[0].strip()


CIAN_TYPE = {"flat": "offer_type=flat",
             "parking": "offer_type=offices&office_type%5B0%5D=6&garage_type%5B0%5D=1"}   # гараж → машино-место
AVITO_CAT = {"flat": "kvartiry/prodam", "parking": "garazhi_i_mashinomesta/prodam"}


def search_links(lot: dict) -> list[tuple[str, str]]:
    """Ссылки на поиск объявлений рядом с лотом: ЦИАН — карта вокруг дома (у квартир — площадь ±20 %),
    Авито — по улице; у машино-мест — раздел машино-мест."""
    kind = lot.get("kind") or "flat"
    out = []
    coords = (lot.get("coords") or "").replace(" ", "")
    if re.fullmatch(r"\d+\.\d+,\d+\.\d+", coords):
        url = (f"https://www.cian.ru/map/?deal_type=sale&engine_version=2&{CIAN_TYPE[kind]}&region=1"
               f"&center={coords}&zoom=16")
        space = lot.get("space") or 0
        if kind == "flat" and space:
            url += f"&minarea={round(space * 0.8)}&maxarea={round(space * 1.2)}"
        out.append(("ЦИАН — рядом", url))
    street = _street(lot.get("place"))
    if street:
        out.append(("Авито — улица", f"https://www.avito.ru/moskva/{AVITO_CAT[kind]}?q={quote(street)}"))
    return out


BUTTONS_TEXT = ("Проверить рынок рядом. Ответьте на черновик или на это сообщение ссылками на объявления — "
                "по строке: ссылка, цена, площадь")


def reply_markup(lot: dict) -> str | None:
    links = search_links(lot)
    if not links:
        return None
    return json.dumps({"inline_keyboard": [[{"text": label, "url": url} for label, url in links]]}, ensure_ascii=False)


def lot_for_market(r: dict, below: bool | None = None) -> dict | None:
    """Поля лота для проверки рынка — только лот в продаже со стартом ниже медианы (below; по умолчанию —
    discount > 0, то есть дешевле медианы района). Квартиры и машино-места; иначе None."""
    if r.get("kind") not in ("flat", "parking") or not r.get("price"):
        return None
    if r["kind"] == "flat" and not r.get("space"):
        return None
    if not (below if below is not None else (r.get("discount") or 0) > 0):
        return None
    keep = ("kind", "address", "place", "district", "adm", "price", "space", "url", "end_d", "trades_d", "coords")
    return {k: (str(r[k]) if hasattr(r.get(k), "isoformat") else r.get(k)) for k in keep}


# ---------------------------------------------------------------- 2. память кухни

def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def remember(state_dir, message_id: int, key: str, rubric: str, lot: dict, today: date) -> None:
    """Запомнить отправленный черновик с кнопками: ответ на него потом найдём по id сообщения."""
    path = Path(state_dir) / KITCHEN
    data = _load(path, {})
    data[str(message_id)] = {"key": key, "rubric": rubric, "sent": today.isoformat(), "lot": lot}
    cutoff = today.toordinal() - KEEP_DAYS
    data = {k: v for k, v in data.items() if date.fromisoformat(v["sent"]).toordinal() >= cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- 3. разбор ответа автора

URL = re.compile(r"https?://\S+")
NUM = re.compile(r"(\d+(?:[  ]\d{3})*(?:[.,]\d+)?)\s*(млн|м(?![²2]|\s*кв)|mln|тыс|т\.?р|k)?\.?\s*(м²|м2|кв\.?\s*м|кв|m2)?",
                 re.I)


def _num(s: str) -> float:
    return float(s.replace(" ", "").replace(" ", "").replace(",", "."))


def parse_line(line: str) -> dict | None:
    """«ссылка цена площадь» → {url, site, price, area, m2}; None — если в строке нет ссылки."""
    m = URL.search(line)
    if not m:
        return None
    url = m.group(0).rstrip(").,;")
    rest = (line[:m.start()] + " " + line[m.end():]).strip()
    price = area = None
    for num, unit, sq in NUM.findall(rest):
        v = _num(num)
        unit = (unit or "").lower()
        if sq:
            area = v
        elif unit in ("млн", "м", "mln"):
            price = v * 1e6
        elif unit.startswith(("тыс", "т", "k")):
            price = v * 1e3
        elif v >= 100_000 and price is None:
            price = v
        elif area is None and v < 1000:
            area = v
        elif price is None:
            price = v
    host = (urlparse(url).hostname or "").removeprefix("www.").removeprefix("m.")
    site = next((name for dom, name in SITES.items() if host == dom or host.endswith("." + dom)), host)
    if site in SITES.values():   # у площадок адрес объявления — путь; хвосты поиска и слежения не нужны
        url = url.split("?")[0].split("#")[0]
    out = {"url": url, "site": site, "price": price, "area": area}
    if price and area:
        out["m2"] = price / area
    return out


PARKING_RANGE = (100_000, 30_000_000)   # правдоподобная цена машино-места


def parse_listings(text: str, kind: str = "flat") -> tuple[list[dict], list[str]]:
    """Объявления из ответа автора и понятные ошибки по строкам. У квартир нужны цена и площадь,
    у машино-мест — цена (площадь по желанию)."""
    items, errors = [], []
    for i, line in enumerate((text or "").splitlines(), 1):
        if not line.strip():
            continue
        it = parse_line(line)
        if it is None:
            continue   # строка без ссылки — комментарий автора, не ошибка
        if kind == "parking":
            if not it["price"]:
                errors.append(f"строка {i}: нужна цена после ссылки, например «{it['url']} 2,3 млн»")
            elif not PARKING_RANGE[0] <= it["price"] <= PARKING_RANGE[1]:
                errors.append(f"строка {i}: цена {t.money(it['price'])} неправдоподобна для машино-места")
            else:
                items.append(it)
        elif not it["price"] or not it["area"]:
            errors.append(f"строка {i}: нужна цена и площадь после ссылки, например «{it['url']} 14,5 млн 42»")
        elif not M2_RANGE[0] <= it["m2"] <= M2_RANGE[1]:
            errors.append(f"строка {i}: выходит {t.rub(it['m2'])}/м² — проверьте цену и площадь")
        else:
            items.append(it)
    return items, errors


# ---------------------------------------------------------------- 4. сравнение, факты, карточка

def _k(v: float) -> str:
    return f"{round(v / 1000):,}".replace(",", " ")


def _is_flat(lot: dict) -> bool:
    return (lot.get("kind") or "flat") == "flat"


def compare(lot: dict, items: list[dict]) -> dict:
    """Квартиры сравниваются по цене за м², машино-места — по цене лота."""
    flat = _is_flat(lot)
    vals = [x["m2"] if flat else x["price"] for x in items]
    base = lot["price"] / lot["space"] if flat else lot["price"]
    med = statistics.median(vals)
    return {"n": len(items), "min": min(vals), "max": max(vals), "median": med, "base": base, "flat": flat,
            "gap": 1 - base / med, "gap_min": 1 - base / min(vals), "sites": sorted({x["site"] for x in items})}


def _range(c: dict) -> str:
    if c["flat"]:
        lo, hi = _k(c["min"]), _k(c["max"])
        return f"{lo}–{hi} тыс. ₽/м²" if lo != hi else f"{lo} тыс. ₽/м²"
    lo, hi = t.money(c["min"]), t.money(c["max"])
    if lo == hi:
        return lo
    if lo.endswith(" млн ₽") and hi.endswith(" млн ₽"):
        return f"{lo.removesuffix(' млн ₽')}–{hi}"
    return f"{lo} – {hi}"


def facts(lot: dict, items: list[dict], c: dict, checked: date) -> dict:
    flat = c["flat"]
    what = "квартир" if flat else "машино-мест"
    f = {"address": lot.get("place"), "district": t.district(lot.get("district")),
         "start_price": t.money(lot["price"]),
         "start_price_per_m2": f"{t.rub(lot['price'] / lot['space'])}/м²" if lot.get("space") else None,
         "area": t.area(lot.get("space")), "applications_until": t.day(lot.get("end_d")),
         "trades_date": t.day(lot.get("trades_d")), "lot_url": lot.get("url"),
         "compared_by": "цена за м²" if flat else "цена лота",
         "listings_count": f"{c['n']} {t.plural(c['n'], ('объявление', 'объявления', 'объявлений'))}",
         "listings_range": _range(c),
         "listings_median": f"{t.rub(c['median'])}/м²" if flat else t.money(c["median"]),
         "listings_sites": ", ".join(c["sites"]), "listings_checked": t.day(checked),
         "listings_what": f"объявления о продаже похожих {what} рядом, которые автор канала подобрал вручную; "
                          "это цены, которые просят продавцы, а не цены сделок, и это не полный обзор рынка",
         "listings": [{k: v for k, v in {"url": x["url"], "site": x["site"], "price": t.money(x["price"]),
                                         "area": t.area(x["area"]) if x.get("area") else None,
                                         "price_per_m2": f"{t.rub(x['m2'])}/м²" if x.get("m2") else None}.items() if v}
                      for x in items],
         "compare_note": "сравнивается стартовая цена лота с ценами в объявлениях; на аукционе цена может вырасти"}
    if c["gap"] > 0:
        f["below_listings_median"] = f"{t.pct(c['gap'])} %"
        diff = c["gap"] * c["median"] * (lot["space"] if flat else 1)
        f["difference_to_median"] = t.money(diff)
    else:
        f["above_listings_median"] = f"{t.pct(-c['gap'])} %"
    if c["gap_min"] > 0:
        f["below_cheapest_listing"] = f"{t.pct(c['gap_min'])} %"
    return {"rubric": "market", "rubric_title": "Рядом на рынке", "object": "квартира" if flat else "машино-место",
            "facts": {k: v for k, v in f.items() if v not in (None, "", [])}}


def card(lot: dict, c: dict) -> dict:
    sign = "−" if c["gap"] > 0 else "+"
    if c["flat"]:
        sub, start = "старт за м² к объявлениям рядом", f"старт {t.rub(c['base'])}/м² · {t.area(lot['space'])}"
    else:
        sub, start = "старт к объявлениям рядом", t.area(lot.get("space"))
    return t._card("lot", "РЯДОМ НА РЫНКЕ", type="flat" if c["flat"] else "parking",
                   headline=f"{sign}{t.pct(abs(c['gap']))}%", headline_sub=sub,
                   deadline=f"заявки до {t.day(lot['end_d'])}" if lot.get("end_d") else "",
                   price=t.money(lot["price"]),
                   price_sub=" · ".join(x for x in (start, f"в объявлениях {_range(c)} ({c['n']})") if x),
                   address=lot.get("place"), district=lot.get("district"))


def text(lot: dict, items: list[dict], c: dict, checked: date) -> str:
    """Шаблонный текст — запасной, если Claude не написал свой."""
    links = ", ".join(f'<a href="{x["url"]}">{x["site"]}</a>' for x in items[:6])
    word = t.plural(c["n"], ("объявление", "объявления", "объявлений"))
    what = "квартир" if c["flat"] else "машино-мест"
    start = f"Старт {t.rub(c['base'])}/м²" if c["flat"] else f"Старт {t.money(c['base'])}"
    return t._post([
        f"🔎 <b>{start}, а в объявлениях рядом — {_range(c)}</b>", "",
        t._where(lot.get("place"), lot.get("district")),
        f"💰 <b>{t.money(lot['price'])}</b>" + (f" · {t.area(lot['space'])}" if lot.get("space") else ""),
        f"📊 {c['n']} {word} о продаже похожих {what} рядом ({t.day(checked)}): {links}",
        "Это цены, которые просят продавцы, и стартовая цена лота: на аукционе она может вырасти.",
        f"⏳ Заявки до {t.day(lot.get('end_d'))}" if lot.get("end_d") else None,
        t.link(lot.get("url")),
    ], ["#рядомнарынке", "#квартиры" if c["flat"] else "#машиноместа"])


# ---------------------------------------------------------------- 5. забрать ответы из кухни

def collect(bot, state_dir, chat: str, today: date) -> tuple[list[dict], list[tuple[int, str]]]:
    """Ответы автора на черновики с кнопками: [{message_id, reply_id, entry, items}], [(reply_id, ошибки)].

    Смещение getUpdates сохраняется сразу — ответ не обработается дважды; ошибки бот называет в кухне."""
    state_dir = Path(state_dir)
    kitchen = _load(state_dir / KITCHEN, {})
    offset = _load(state_dir / UPDATES, {}).get("offset")
    updates = bot.call("getUpdates", {"offset": offset, "timeout": "0", "limit": "100",
                                      "allowed_updates": json.dumps(["channel_post", "message"])})
    found, problems = [], []
    for u in updates:
        offset = u["update_id"] + 1
        msg = u.get("channel_post") or u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(chat) and msg.get("chat", {}).get("username") != str(chat).lstrip("@"):
            continue
        to = (msg.get("reply_to_message") or {}).get("message_id")
        entry = kitchen.get(str(to)) if to else None
        if not entry:
            continue
        items, errors = parse_listings(msg.get("text") or msg.get("caption") or "", entry["lot"].get("kind") or "flat")
        if errors:
            problems.append((msg["message_id"], errors))
        if items:
            found.append({"message_id": to, "reply_id": msg["message_id"], "entry": entry, "items": items})
        elif not errors:
            problems.append((msg["message_id"], ["не нашёл ни одной ссылки на объявление"]))
    if updates:
        (state_dir / UPDATES).write_text(json.dumps({"offset": offset, "at": today.isoformat()}), encoding="utf-8")
    return found, problems
