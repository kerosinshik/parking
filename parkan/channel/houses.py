"""Дома с пачками лотов на торгах — готовые сообщения для чатов домов и ЖК.

Город часто продаёт машино-места пачкой в одном доме, а покупать их будут прежде всего жители этого дома.
Сообщение в чат дома — самый точный способ найти первых пользователей бота: людям это действительно нужно,
а ссылка ведёт на карточку самого дешёвого лота дома (`?start=dom_<id>`, метка видна в /stats бота).

Отправляет автор вручную, соблюдая правила чата. Текст — факты без рекламных слов; от имени жильца не пишем.
"""

from __future__ import annotations

from datetime import date

from . import lots, templates as t

MIN_LOTS = {"parking": 5, "flat": 3}


def query(con, min_lots: dict = MIN_LOTS, top: int | None = None) -> list[dict]:
    """Дома, где сейчас принимают заявки хотя бы на min_lots лотов одного типа (таблицы lots.load уже есть);
    самые большие пачки — первыми."""
    rows = lots._finish(lots._rows(con, """
        SELECT kind, address, any_value(district) AS district, any_value(adm) AS adm, count(*) AS n,
               min(price) AS price_min, max(price) AS price_max, min(space) AS space_min, max(space) AS space_max,
               min(end_d) AS end_d, min(trades_d) AS trades_d, arg_min(lot_id, price) AS lot_id,
               arg_min(site, price) AS site, any_value(ref) AS ref, any_value(scope) AS scope
        FROM ch_sale GROUP BY kind, address ORDER BY n DESC, price_min
    """))
    rows = [r for r in rows if r["n"] >= min_lots.get(r["kind"], 5)]
    return rows[:top] if top else rows


def find(con, state_dir, today: date, min_lots: dict = MIN_LOTS, top: int = 15) -> list[dict]:
    if not lots.load(con, state_dir, today):
        raise RuntimeError(f"нет журнала набора № 1461 в {state_dir} (сначала track-sync)")
    return query(con, min_lots, top)


def cheaper(h: dict) -> bool:
    """Есть ли что сравнивать: самый дешёвый лот дома заметно ниже медианы (больше 10 %)."""
    return bool(h.get("ref")) and h["kind"] == "parking" and h["price_min"] < h["ref"] * 0.9


def message(h: dict, bot_url: str | None) -> str:
    """Текст для чата дома: что продаётся, почём, до какого числа, ссылка на лот и на бота."""
    w = t.WORDS[h["kind"]]
    prices = f"от {t.money(h['price_min'])} до {t.money(h['price_max'])}" if h["price_max"] > h["price_min"] \
        else t.money(h["price_min"])
    area = t.area_range(h.get("space_min"), h.get("space_max"))
    lines = [f"В этом доме ({h['place']}) город продаёт на торгах {h['n']} {t.plural(h['n'], w['forms'])}: "
             f"{prices}" + (f", {area}" if area else "") + "."]
    if h.get("end_d"):
        lines.append(f"Заявки принимают до {t.day(h['end_d'])}"
                     + (f", торги {t.day(h['trades_d'])}." if h.get("trades_d") else "."))
    if cheaper(h):
        lines.append(f"Для сравнения: медиана по лотам {h.get('scope') or 'района'} — {t.money(h['ref'])}.")
    if h.get("url"):
        lines.append(f"Самый дешёвый лот на сайте торгов: {h['url']}")
    if bot_url:
        lines.append(f"Все лоты дома и уведомления о новых: {bot_url.rstrip('/')}?start=dom_{h['lot_id']}")
    return "\n".join(lines)


def report(houses: list[dict], bot_url: str | None, today: date) -> str:
    out = [f"# Дома с пачками лотов на {t.day(today)}", "",
           "Готовые сообщения для чатов домов и ЖК. Чат дома обычно находится поиском Telegram по адресу или "
           "названию ЖК. Перед отправкой прочитайте правила чата; если реклама запрещена, спросите администратора.", ""]
    if not bot_url:
        out += ["> Ссылки на бота нет: задайте PARKAN_BOT_URL или --bot-url.", ""]
    for i, h in enumerate(houses, 1):
        out += [f"## {i}. {h['place']} — {h['n']} {t.plural(h['n'], t.WORDS[h['kind']]['forms'])}",
                f"{t.district(h.get('district'))}, {h.get('adm') or ''}".strip(", "), "", "```", message(h, bot_url),
                "```", ""]
    if not houses:
        out.append("Сейчас нет домов с пачками лотов.")
    return "\n".join(out)
