"""Тексты постов (HTML для Telegram) и данные для карточек.

Строка «✏️ …» в конце черновика — место для комментария редактора: перед публикацией её
заменяют своей фразой или удаляют.
"""

from __future__ import annotations

import os
import re
from html import escape

from ..track.digest import _addr

EDITOR = "✏️ <i>ваш комментарий</i>"


def channel_name() -> str:
    return os.environ.get("PARKAN_CHANNEL_NAME", "Попутный Метр")


def mln(v) -> str:
    return f"{float(v) / 1e6:.2f}".replace(".", ",") + " млн ₽"


def rub(v) -> str:
    return f"{round(float(v)):,}".replace(",", " ") + " ₽"


def num(v, nd: int = 1) -> str:
    return f"{float(v):.{nd}f}".rstrip("0").rstrip(".").replace(".", ",")


def district(s: str | None) -> str:
    return re.sub(r"^район ", "", s or "").strip()


def ru_date(s: str | None) -> str:
    """2026-10-02 → 02.10.2026 (таблица итогов хранит даты в ISO)."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s or "")
    return f"{m[3]}.{m[2]}.{m[1]}" if m else (s or "")


def link(url: str | None, label: str = "Лот на torgi.mos.ru") -> str:
    return f'<a href="{escape(url, quote=True)}">{label}</a>' if url else ""


def _join(lines) -> str:
    return "\n".join(l for l in lines if l is not None)


# ---------------------------------------------------------------- «Лот дня»

def lot(f) -> str:
    d = f.data
    if f.kind == "снижение цены":
        head = f"📉 <b>Стартовая цена снижена на {round(d['drop'] * 100)} %</b>"
        price = f"💰 {mln(d['old_price'])} → <b>{mln(d['price'])}</b>"
        extra = None
    else:
        head = (f"🅿️ <b>Машино-место на {round(d['discount'] * 100)} % дешевле района</b>" if "discount" in d
                else "🅿️ <b>Среди 5 % самых дешёвых лотов по цене за м²</b>")
        price = f"💰 <b>{mln(d['price'])}</b>" + (f" · медиана района {mln(d['median'])}" if d.get("median") else "")
        extra = " · ".join(x for x in (f"{num(d['space'])} м²" if d.get("space") else "",
                                       f"{rub(d['m2'])}/м²" if d.get("m2") else "",
                                       f"лотов по адресу: {d['lots']}" if (d.get("lots") or 1) > 1 else "") if x)
        extra = f"📐 {extra}" if extra else None
    return _join([
        head, "",
        f"📍 {escape(d['address'])}" + (f" ({escape(district(d.get('district')))})" if d.get("district") else ""),
        price, extra,
        f"⏳ Заявки до {d['until']}" if d.get("until") else None,
        link(d.get("url")), "", EDITOR, "",
        "#лотдня",
    ])


def lot_card(f) -> dict:
    d = f.data
    if f.kind == "снижение цены":
        big, sub = f"−{round(d['drop'] * 100)} %", f"{mln(d['old_price'])} → {mln(d['price'])}"
        label = "СНИЖЕНИЕ ЦЕНЫ"
    elif "discount" in d:
        big, sub = f"−{round(d['discount'] * 100)} %", f"{mln(d['price'])} при медиане района {mln(d['median'])}"
        label = "ЛОТ ДНЯ"
    else:
        big, sub = rub(d["m2"]) + "/м²", f"{mln(d['price'])} · среди 5 % самых дешёвых"
        label = "ЛОТ ДНЯ"
    return {"label": label, "big": big, "sub": sub, "place": d["address"], "district": district(d.get("district")),
            "badge": f"заявки до {d['until']}" if d.get("until") else ""}


# ---------------------------------------------------------------- «Итоги»

def _place(r: dict) -> str:
    return _addr(r.get("address") or r.get("display_address"))


def result(r: dict) -> str:
    start, fin, ratio = float(r["start_price"]), float(r["final_price"]), float(r["final_to_start"])
    sub = re.sub(r";.*$", "", r.get("subway") or "")
    sub = re.sub(r" \(None мин пешком, None км\)", "", sub)
    sub = re.sub(r"(\d)\.(\d)", r"\1,\2", sub)   # 0.59 км → 0,59 км
    space = f"{num(r['space_m2'])} м²" if r.get("space_m2") else ""
    m2 = f"{rub(r['final_price_per_m2'])}/м²" if r.get("final_price_per_m2") else ""
    return _join([
        f"🔨 <b>Продано в {num(ratio)} раза дороже старта</b>", "",
        f"📍 {escape(_place(r))}" + (f" ({escape(district(r.get('district')))})" if r.get("district") else ""),
        f"🚇 {escape(sub)}" if sub else None,
        f"💰 {mln(start)} → <b>{mln(fin)}</b>",
        f"📐 {' · '.join(x for x in (space, m2) if x)}" if space or m2 else None,
        f"🗓 Торги {ru_date(r['trades_date'])}" if r.get("trades_date") else None,
        " · ".join(x for x in (link(r.get("url")), link(r.get("auction_protocol_url"), "протокол")) if x), "",
        EDITOR, "",
        "#итоги",
    ])


def result_card(r: dict) -> dict:
    return {"label": "ИТОГИ ТОРГОВ", "big": f"×{num(r['final_to_start'])}",
            "sub": f"{mln(r['start_price'])} → {mln(r['final_price'])}", "place": _place(r),
            "district": district(r.get("district")), "badge": f"торги {ru_date(r['trades_date'])}" if r.get("trades_date") else ""}


# ---------------------------------------------------------------- «Неделя вперёд»

def week(lots: list[dict], today) -> str:
    lines = ["🗓 <b>Неделя вперёд: успеть подать заявку</b>",
             "Лоты дешевле медианы своего района, приём заявок по которым заканчивается в ближайшие 7 дней.", ""]
    for i, l in enumerate(lots, 1):
        lines.append(f"{i}. <b>−{round(l['discount'] * 100)} %</b> · {mln(l['price'])}"
                     + (f" · {num(l['space'])} м²" if l.get("space") else "")
                     + f" · до {l['end']}\n   {escape(_addr(l['address']))}"
                     + (f" ({escape(district(l.get('district')))})" if l.get("district") else "")
                     + (f" · {link(l.get('url'), 'лот')}" if l.get("url") else ""))
    lines += ["", EDITOR, "", "#неделявперёд"]
    return "\n".join(lines)
