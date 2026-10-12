"""Тексты постов (HTML для Telegram) и данные карточек для всех рубрик канала.

Строка «✏️ ваш комментарий» в конце черновика — место для фразы автора: перед публикацией её
заменяют своей или удаляют. Карточка — словарь для html_cards: template (lot | list | stats) + поля макета.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta
from html import escape

EDITOR = "✏️ <i>ваш комментарий</i>"
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря")

WORDS = {
    "parking": {"one": "Машино-место", "forms": ("машино-место", "машино-места", "машино-мест"), "sold": "продано",
                "tag": "#машиноместа", "icon": "🅿️"},
    "flat": {"one": "Квартира", "forms": ("квартира", "квартиры", "квартир"), "sold": "продана",
             "tag": "#квартиры", "icon": "🏠"},
}


def channel_name() -> str:
    return os.environ.get("PARKAN_CHANNEL_NAME", "Попутный Метр")


# ---------------------------------------------------------------- форматирование

def plural(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def num(v, nd: int = 1) -> str:
    return f"{float(v):.{nd}f}".rstrip("0").rstrip(".").replace(".", ",")


def money(v) -> str:
    """504 тыс. ₽ · 1,29 млн ₽ · 152,4 млн ₽."""
    v = float(v)
    if v < 1e6:
        return f"{round(v / 1000)} тыс. ₽"
    return (f"{v / 1e6:.2f}" if v < 1e8 else f"{v / 1e6:.1f}").replace(".", ",") + " млн ₽"


def rub(v) -> str:
    return f"{round(float(v)):,}".replace(",", " ") + " ₽"


def area(v) -> str:
    return f"{num(v)} м²" if v else ""


def area_range(a, b) -> str:
    if not a:
        return ""
    return area(a) if not b or round(a, 1) == round(b, 1) else f"{num(a)}–{num(b)} м²"


def pct(x: float) -> str:
    return f"{round(x * 100)}"


def district(s: str | None) -> str:
    return re.sub(r"^район ", "", s or "").strip()


def to_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(v)[:10], fmt).date()
        except (TypeError, ValueError):
            pass
    return None


def day(v) -> str:
    """9 ноября."""
    d = to_date(v)
    return f"{d.day} {MONTHS[d.month - 1]}" if d else ""


def short_day(v) -> str:
    d = to_date(v)
    return f"{d:%d.%m}" if d else ""


def link(url: str | None, label: str = "Лот на torgi.mos.ru") -> str:
    return f'<a href="{escape(url, quote=True)}">{label}</a>' if url else ""


def _where(place: str | None, dist: str | None) -> str:
    return f"📍 {escape(place or '')}" + (f" ({escape(district(dist))})" if dist else "")


def _subway(s: str | None) -> str | None:
    s = re.sub(r";.*$", "", s or "")
    s = re.sub(r" \(None мин пешком, None км\)", "", s)
    s = re.sub(r"(\d)\.(\d)", r"\1,\2", s)
    return f"🚇 {escape(s)}" if s else None


def _post(lines, tags=None) -> str:
    """Запасной шаблонный текст (если написанный не прошёл проверку). Хэштеги не ставим — решение 12.10.2026."""
    return "\n".join(l for l in [*lines, "", EDITOR] if l is not None)


def _card(template: str, kicker: str, **fields) -> dict:
    return {"template": template, "kicker": kicker, "brand": channel_name().upper(), **fields}


def _ref_text(r: dict) -> str:
    """«медиана района 2,30 млн ₽» / «медиана района 456 576 ₽/м²»."""
    if not r.get("ref"):
        return ""
    ref = money(r["ref"]) if r["kind"] == "parking" else rub(r["ref"]) + "/м²"
    return f"медиана {r.get('scope') or 'района'} {ref}"


def _price_line(r: dict) -> str:
    parts = [f"<b>{money(r['price'])}</b>"]
    if r["kind"] == "flat" and r.get("space"):
        parts.append(f"{rub(r['price'] / r['space'])}/м²")
    if r.get("ref"):
        parts.append(_ref_text(r))
    return "💰 " + " · ".join(parts)


# ---------------------------------------------------------------- 1. Лот дня

def lot(r: dict) -> str:
    w = WORDS[r["kind"]]
    tail = " за м²" if r["kind"] == "flat" else ""
    extra = [area(r.get("space"))]
    if r["kind"] == "parking" and r.get("space"):
        extra.append(f"{rub(r['price'] / r['space'])}/м²")
    if (r.get("at_addr") or 1) > 1:
        extra.append(f"лотов по адресу: {r['at_addr']}")
    extra = " · ".join(x for x in extra if x)
    return _post([
        f"{w['icon']} <b>{w['one']} на {pct(r['discount'])} % дешевле {r.get('scope') or 'района'}{tail}</b>", "",
        _where(r["place"], r.get("district")), _price_line(r),
        f"📐 {extra}" if extra else None,
        f"⏳ Заявки до {day(r.get('end_d'))}" if r.get("end_d") else None,
        link(r.get("url")),
    ], ["#лотдня", w["tag"]])


def lot_card(r: dict) -> dict:
    sub = "дешевле района за м²" if r["kind"] == "flat" else "дешевле медианы района"
    if r.get("scope") == "округа":
        sub = sub.replace("района", "округа")
    price_sub = [_ref_text(r).replace("медиана района ", "медиана — ").replace("медиана округа ", "медиана округа — ")]
    price_sub.append(area(r.get("space")))
    return _card("lot", "ЛОТ ДНЯ", type=r["kind"], headline=f"−{pct(r['discount'])}%", headline_sub=sub,
                 deadline=f"заявки до {day(r['end_d'])}" if r.get("end_d") else "", price=money(r["price"]),
                 price_sub=" · ".join(x for x in price_sub if x), address=r["place"], district=r.get("district"))


# ---------------------------------------------------------------- 2. Снижение цены

def drop(r: dict) -> str:
    w = WORDS[r["kind"]]
    return _post([
        f"📉 <b>Город снизил стартовую цену на {pct(r['drop'])} %</b>", "",
        f"{w['icon']} {w['one']}", _where(r["place"], r.get("district")),
        f"💰 {money(r['old_price'])} → <b>{money(r['price'])}</b>",
        f"📐 {area(r['space'])}" + (f" · {rub(r['price'] / r['space'])}/м²" if r.get("space") else "") if r.get("space") else None,
        f"⏳ Заявки до {day(r.get('end_d'))}" if r.get("end_d") else None,
        link(r.get("url")),
    ], ["#снижение", w["tag"]])


def drop_card(r: dict) -> dict:
    return _card("lot", "СНИЖЕНИЕ ЦЕНЫ", type=r["kind"], headline=f"−{pct(r['drop'])}%",
                 headline_sub="город снизил стартовую цену",
                 deadline=f"заявки до {day(r['end_d'])}" if r.get("end_d") else "", price=money(r["price"]),
                 old_price=money(r["old_price"]), price_sub=area(r.get("space")), address=r["place"],
                 district=r.get("district"))


# ---------------------------------------------------------------- 3. Целый дом

def house(r: dict) -> str:
    w = WORDS[r["kind"]]
    prices = f"от {money(r['price_min'])} до {money(r['price_max'])}" if r["price_max"] > r["price_min"] \
        else money(r["price_min"])
    ref = _ref_text(dict(r, ref=r.get("ref"))) if r.get("ref") else ""
    return _post([
        f"🏢 <b>{r['n']} {plural(r['n'], w['forms'])} в одном доме</b>", "",
        _where(r["place"], r.get("district")),
        "💰 " + " · ".join(x for x in (prices, ref) if x),
        f"📐 {area_range(r.get('space_min'), r.get('space_max'))}" if r.get("space_min") else None,
        f"⏳ Заявки до {day(r.get('end_d'))}" if r.get("end_d") else None,
        link(r.get("url"), "Самый дешёвый лот дома"),
    ], ["#целыйдом", w["tag"]])


def house_card(r: dict) -> dict:
    w = WORDS[r["kind"]]
    sub = [f"до {money(r['price_max'])}"] if r["price_max"] > r["price_min"] else []
    sub.append(area_range(r.get("space_min"), r.get("space_max")))
    return _card("lot", "ЦЕЛЫЙ ДОМ", type=r["kind"], headline=str(r["n"]),
                 headline_sub=f"{plural(r['n'], w['forms'])} в одном доме на торгах",
                 deadline=f"заявки до {day(r['end_d'])}" if r.get("end_d") else "",
                 price=f"от {money(r['price_min'])}" if r["price_max"] > r["price_min"] else money(r["price_min"]),
                 price_sub=" · ".join(x for x in sub if x), address=r["place"], district=r.get("district"))


# ---------------------------------------------------------------- 4. Продано дороже

def sold(r: dict) -> str:
    w = WORDS[r["kind"]]
    m2 = f"{rub(r['final_price_per_m2'])}/м²" if r.get("final_price_per_m2") else ""
    return _post([
        f"🔨 <b>{w['one']} {w['sold']} в {num(r['final_to_start'])} раза дороже старта</b>", "",
        _where(r["place"], r.get("district")), _subway(r.get("subway")),
        f"💰 {money(r['start_price'])} → <b>{money(r['final_price'])}</b>",
        f"📐 {' · '.join(x for x in (area(r.get('space_m2')), m2) if x)}" if r.get("space_m2") or m2 else None,
        f"🗓 Торги {day(r.get('trades_date'))}" if r.get("trades_date") else None,
        " · ".join(x for x in (link(r.get("url")), link(r.get("auction_protocol_url"), "протокол")) if x),
    ], ["#итоги", w["tag"]])


def sold_card(r: dict) -> dict:
    return _card("lot", "ИТОГИ ТОРГОВ", type=r["kind"], headline=f"×{num(r['final_to_start'])}",
                 headline_sub="продано дороже старта", deadline=f"торги {day(r['trades_date'])}",
                 price=money(r["final_price"]),
                 price_sub=" · ".join(x for x in (f"старт — {money(r['start_price'])}", area(r.get("space_m2"))) if x),
                 address=r["place"], district=r.get("district"))


# ---------------------------------------------------------------- 5. Без борьбы

def nofight(r: dict, same: int) -> str:
    """same — сколько ещё лотов этого дома в тот же день ушли так же."""
    w = WORDS[r["kind"]]
    m2 = f"{rub(r['final_price'] / r['space_m2'])}/м²" if r.get("space_m2") else ""
    return _post([
        f"🤫 <b>Один участник — {w['one'].lower()} {w['sold']} по стартовой цене</b>", "",
        _where(r["place"], r.get("district")), _subway(r.get("subway")),
        f"💰 <b>{money(r['final_price'])}</b> — ни рубля сверху",
        f"📐 {' · '.join(x for x in (area(r.get('space_m2')), m2) if x)}" if r.get("space_m2") else None,
        f"🗓 Торги {day(r.get('trades_date'))}" if r.get("trades_date") else None,
        f"В этом доме так же ушли ещё {same} {plural(same, w['forms'])}." if same else None,
        " · ".join(x for x in (link(r.get("url")), link(r.get("auction_protocol_url"), "протокол")) if x),
    ], ["#безборьбы", w["tag"]])


def nofight_card(r: dict, same: int) -> dict:
    w = WORDS[r["kind"]]
    sub = [area(r.get("space_m2"))]
    if same:
        sub.append(f"ещё {same} {plural(same, w['forms'])} в доме так же")
    return _card("lot", "БЕЗ БОРЬБЫ", type=r["kind"], headline="+0 ₽",
                 headline_sub="один участник — продано по стартовой цене", deadline=f"торги {day(r['trades_date'])}",
                 price=money(r["final_price"]), price_sub=" · ".join(x for x in sub if x), address=r["place"],
                 district=r.get("district"))


# ---------------------------------------------------------------- 6. Неделя вперёд

def week(kind: str, lots: list[dict], today: date) -> str:
    w = WORDS[kind]
    lines = [f"🗓 <b>Неделя вперёд: {w['forms'][1]}</b>",
             "Лоты дешевле медианы района, приём заявок по которым закрывается в ближайшие 7 дней.", ""]
    for i, l in enumerate(lots, 1):
        lines.append(f"{i}. <b>−{pct(l['discount'])} %</b> · {money(l['price'])}"
                     + (f" · {area(l['space'])}" if l.get("space") else "")
                     + f" · до {day(l['end_d'])}\n   {escape(l['place'])}"
                     + (f" ({escape(district(l.get('district')))})" if l.get("district") else "")
                     + (f" · {link(l.get('url'), 'лот')}" if l.get("url") else ""))
    return _post(lines, ["#неделявперёд", w["tag"]])


def week_card(kind: str, lots: list[dict], today: date) -> dict:
    w = WORDS[kind]
    n = len(lots)
    return _card("list", "НЕДЕЛЯ ВПЕРЁД", headline=str(n),
                 headline_sub=f"{plural(n, w['forms'])} дешевле района — заявки закрываются на этой неделе",
                 deadline=f"до {day(today + timedelta(days=7))}", districts=[l.get("district") for l in lots],
                 rows=[{"lead": f"−{pct(l['discount'])}%", "price": money(l["price"]),
                        "place": district(l.get("district")), "until": f"до {short_day(l['end_d'])}"} for l in lots])


# ---------------------------------------------------------------- 7. Итоги недели

def weekstats(kind: str, st: dict) -> str:
    w = WORDS[kind]
    rec = st.get("record")
    lines = [f"📊 <b>Неделя на торгах: {w['forms'][1]}, {st['period']}</b>", "",
             f"🔨 Продано: {st['sold']} из {st['total']} торгов",
             f"🤫 По стартовой цене, без торга: {st['at_start']} ({pct(st['at_start'] / st['sold'])} %)" if st["sold"] else None,
             f"📈 Рекорд: ×{num(rec['final_to_start'])} — {escape(rec['place'])} ({escape(district(rec.get('district')))}), "
             f"{money(rec['start_price'])} → {money(rec['final_price'])}" if rec else None,
             f"🚫 Не состоялись (покупателя не нашлось): {st['failed']} · отменены городом: {st['cancelled']}"]
    return _post(lines, ["#итогинедели", w["tag"]])


def _sold_verb(kind: str, n: int) -> str:
    """81 машино-место продано, 21 квартира продана, 12 квартир продано."""
    return WORDS[kind]["sold"] if n % 10 == 1 and n % 100 != 11 else "продано"


def weekstats_card(kind: str, st: dict) -> dict:
    w = WORDS[kind]
    rec = st.get("record")
    stats = []
    if st["sold"]:
        stats.append({"num": f"{pct(st['at_start'] / st['sold'])}%", "cap": "ушли по стартовой цене — без торга",
                      "lime": True})
    if rec:
        stats.append({"num": f"×{num(rec['final_to_start'])}", "cap": f"рекорд: {district(rec.get('district'))}"})
    return _card("stats", "ИТОГИ НЕДЕЛИ", headline=str(st["sold"]),
                 headline_sub=f"{plural(st['sold'], w['forms'])} {_sold_verb(kind, st['sold'])} на торгах",
                 deadline=st["period"], districts=st["districts"], stats=stats,
                 foot=f"без покупателя — {st['failed']} · отменены городом — {st['cancelled']}")
