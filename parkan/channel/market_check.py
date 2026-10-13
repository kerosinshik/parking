"""«Проверить рынок»: автор вручную подбирает объявления рядом с лотом, бот считает и рисует карточку.

Автоматически собирать объявления ЦИАН, Авито и других площадок нельзя (их правила запрещают роботов и базы
из объявлений), поэтому сравнение с рынком делает человек:

1. Под черновиком квартиры в кухне — кнопки-ссылки на поиск объявлений рядом (search_links). Telegram открывает их
   сам, сервер не нужен. Отправленные черновики запоминаются в channel_state/kitchen.json (id сообщения → лот).
2. Автор смотрит объявления и отвечает (reply) на черновик в кухне, по строке на объявление:
   «https://www.cian.ru/sale/flat/123/ 14,5 млн 42» — ссылка, цена, площадь.
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


def search_links(lot: dict) -> list[tuple[str, str]]:
    """Ссылки на поиск объявлений рядом с лотом: площадь ±20 %, ЦИАН — карта вокруг дома, Авито — по улице."""
    space = lot.get("space") or 0
    lo, hi = (round(space * 0.8), round(space * 1.2)) if space else (None, None)
    out = []
    coords = (lot.get("coords") or "").replace(" ", "")
    if re.fullmatch(r"\d+\.\d+,\d+\.\d+", coords):
        url = (f"https://www.cian.ru/map/?deal_type=sale&engine_version=2&offer_type=flat&region=1"
               f"&center={coords}&zoom=16")
        if lo:
            url += f"&minarea={lo}&maxarea={hi}"
        out.append(("ЦИАН — рядом", url))
    street = _street(lot.get("place"))
    if street:
        out.append(("Авито — улица", f"https://www.avito.ru/moskva/kvartiry/prodam?q={quote(street)}"))
    return out


def reply_markup(lot: dict) -> str | None:
    links = search_links(lot)
    if not links:
        return None
    return json.dumps({"inline_keyboard": [[{"text": label, "url": url} for label, url in links]]}, ensure_ascii=False)


def lot_for_market(r: dict) -> dict | None:
    """Поля лота, нужные для проверки рынка (квартира в продаже); иначе None."""
    if r.get("kind") != "flat" or not r.get("price") or not r.get("space"):
        return None
    keep = ("address", "place", "district", "adm", "price", "space", "url", "end_d", "trades_d", "coords")
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
    out = {"url": url, "site": site, "price": price, "area": area}
    if price and area:
        out["m2"] = price / area
    return out


def parse_listings(text: str) -> tuple[list[dict], list[str]]:
    """Объявления из ответа автора и понятные ошибки по строкам."""
    items, errors = [], []
    for i, line in enumerate((text or "").splitlines(), 1):
        if not line.strip():
            continue
        it = parse_line(line)
        if it is None:
            continue   # строка без ссылки — комментарий автора, не ошибка
        if not it["price"] or not it["area"]:
            errors.append(f"строка {i}: нужна цена и площадь после ссылки, например «{it['url']} 14,5 млн 42»")
        elif not M2_RANGE[0] <= it["m2"] <= M2_RANGE[1]:
            errors.append(f"строка {i}: выходит {t.rub(it['m2'])}/м² — проверьте цену и площадь")
        else:
            items.append(it)
    return items, errors


# ---------------------------------------------------------------- 4. сравнение, факты, карточка

def _k(v: float) -> str:
    return f"{round(v / 1000):,}".replace(",", " ")


def compare(lot: dict, items: list[dict]) -> dict:
    m2 = [x["m2"] for x in items]
    lot_m2 = lot["price"] / lot["space"]
    med = statistics.median(m2)
    return {"n": len(items), "min": min(m2), "max": max(m2), "median": med, "lot_m2": lot_m2,
            "gap": 1 - lot_m2 / med, "gap_min": 1 - lot_m2 / min(m2),
            "sites": sorted({x["site"] for x in items})}


def facts(lot: dict, items: list[dict], c: dict, checked: date) -> dict:
    rng = f"{_k(c['min'])}–{_k(c['max'])} тыс. ₽/м²" if round(c["min"], -3) != round(c["max"], -3) \
        else f"{_k(c['min'])} тыс. ₽/м²"
    f = {"address": lot.get("place"), "district": t.district(lot.get("district")),
         "start_price": t.money(lot["price"]), "start_price_per_m2": f"{t.rub(c['lot_m2'])}/м²",
         "area": t.area(lot.get("space")), "applications_until": t.day(lot.get("end_d")),
         "trades_date": t.day(lot.get("trades_d")), "lot_url": lot.get("url"),
         "listings_count": f"{c['n']} {t.plural(c['n'], ('объявление', 'объявления', 'объявлений'))}",
         "listings_range": rng, "listings_median": f"{t.rub(c['median'])}/м²",
         "listings_sites": ", ".join(c["sites"]), "listings_checked": t.day(checked),
         "listings_what": "объявления о продаже похожих квартир рядом, которые автор канала подобрал вручную; "
                          "это цены, которые просят продавцы, а не цены сделок, и это не полный обзор рынка",
         "listings": [{"url": x["url"], "site": x["site"], "price": t.money(x["price"]), "area": t.area(x["area"]),
                       "price_per_m2": f"{t.rub(x['m2'])}/м²"} for x in items],
         "compare_note": "сравнивается стартовая цена лота с ценами в объявлениях; на аукционе цена может вырасти"}
    if c["gap"] > 0:
        f["below_listings_median"] = f"{t.pct(c['gap'])} %"
        f["difference_to_median"] = t.money(c["gap"] * c["median"] * lot["space"])
    else:
        f["above_listings_median"] = f"{t.pct(-c['gap'])} %"
    if c["gap_min"] > 0:
        f["below_cheapest_listing"] = f"{t.pct(c['gap_min'])} %"
    return {"rubric": "market", "rubric_title": "Рядом на рынке", "object": "квартира",
            "facts": {k: v for k, v in f.items() if v not in (None, "", [])}}


def card(lot: dict, c: dict) -> dict:
    sign = "−" if c["gap"] > 0 else "+"
    return t._card("lot", "РЯДОМ НА РЫНКЕ", type="flat", headline=f"{sign}{t.pct(abs(c['gap']))}%",
                   headline_sub="старт за м² к объявлениям рядом",
                   deadline=f"заявки до {t.day(lot['end_d'])}" if lot.get("end_d") else "",
                   price=t.money(lot["price"]),
                   price_sub=f"старт {t.rub(c['lot_m2'])}/м² · {t.area(lot['space'])} · в объявлениях "
                             f"{_k(c['min'])}–{_k(c['max'])} тыс. ₽/м² ({c['n']})",
                   address=lot.get("place"), district=lot.get("district"))


def text(lot: dict, items: list[dict], c: dict, checked: date) -> str:
    """Шаблонный текст — запасной, если Claude не написал свой."""
    links = ", ".join(f'<a href="{x["url"]}">{x["site"]}</a>' for x in items[:6])
    word = t.plural(c["n"], ("объявление", "объявления", "объявлений"))
    return t._post([
        f"🔎 <b>Старт {t.rub(c['lot_m2'])}/м², а в объявлениях рядом — {_k(c['min'])}–{_k(c['max'])} тыс. ₽/м²</b>", "",
        t._where(lot.get("place"), lot.get("district")),
        f"💰 <b>{t.money(lot['price'])}</b> · {t.area(lot['space'])}",
        f"📊 {c['n']} {word} о продаже похожих квартир рядом ({t.day(checked)}): {links}",
        "Это цены, которые просят продавцы, и стартовая цена лота: на аукционе она может вырасти.",
        f"⏳ Заявки до {t.day(lot.get('end_d'))}" if lot.get("end_d") else None,
        t.link(lot.get("url")),
    ], ["#рядомнарынке", "#квартиры"])


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
        items, errors = parse_listings(msg.get("text") or msg.get("caption") or "")
        if errors:
            problems.append((msg["message_id"], errors))
        if items:
            found.append({"message_id": to, "reply_id": msg["message_id"], "entry": entry, "items": items})
        elif not errors:
            problems.append((msg["message_id"], ["не нашёл ни одной ссылки на объявление"]))
    if updates:
        (state_dir / UPDATES).write_text(json.dumps({"offset": offset, "at": today.isoformat()}), encoding="utf-8")
    return found, problems
