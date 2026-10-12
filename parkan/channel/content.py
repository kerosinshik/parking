"""Отбор материалов для канала: что из данных Пульса идёт в черновики сегодня.

Типы объектов — машино-места и квартиры (Москва и Новая Москва, parkan/channel/lots.py).
Рубрики (docs/channel-design.md):
- lot      — «Лот дня»: новый лот дешевле медианы района (квартиры — за м²);
- drop     — «Снижение цены»: город снизил стартовую цену;
- house    — «Целый дом»: пачка новых лотов в одном доме;
- sold     — «Продано дороже»: итог торгов заметно выше старта;
- nofight  — «Без борьбы»: один участник, продано по стартовой цене;
- week     — «Неделя вперёд» (понедельник): выгодные лоты с дедлайном заявок за 7 дней;
- weekstats — «Итоги недели» (пятница): продано, доля по старту, рекорд.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..track import digest
from . import facts, lots, templates

# Редакционная политика канала. Отдельно от порогов сводки Trello: их можно менять независимо.
POLICY = {
    "lot_min_discount": 0.25,   # «Лот дня»: дешевле медианы минимум на 25 %
    "lots_per_day": 2,
    "drop_min": 0.10,           # «Снижение цены»: минимум на 10 %
    "drops_per_day": 2,
    "house_min_lots": {"parking": 10, "flat": 5},
    "houses_per_day": 1,
    "results_days": 3,          # итоги, проверенные за последние N дней
    "sold_min_ratio": 1.2,      # «Продано дороже»: минимум на 20 % выше старта
    "sold_per_day": 2,
    "nofight_per_day": 1,
    "week_min_discount": 0.15,  # «Неделя вперёд»
    "week_lots": 5,
    "week_min_lots": 3,         # меньше трёх лотов — подборку не делаем
    "week_weekday": 0,          # понедельник
    "weekstats_weekday": 4,     # пятница
    "weekstats_min": 3,         # итогов за неделю по типу, чтобы был пост
}
RUBRICS = ("lot", "drop", "house", "sold", "nofight", "week", "weekstats")


@dataclass
class Post:
    key: str                    # постоянный ключ: один и тот же материал не попадает в черновики дважды
    rubric: str
    text: str                   # HTML для Telegram (parse_mode=HTML)
    card: dict | None = None    # данные для карточки (html_cards), если она нужна
    repeat_days: int = 0        # совместимость с digest.unsent
    score: float = 0.0
    card_path: object = None    # готовая карточка (заполняет channel-draft)
    facts: dict | None = None   # факты для автора текста (facts.py, навык channel-copy)


def _mix(items: list, n: int) -> list:
    """Первые n с чередованием типов: машино-места не вытесняют квартиры и наоборот."""
    by_kind: dict[str, list] = {}
    for it in items:
        by_kind.setdefault(it["kind"], []).append(it)
    out, queues = [], list(by_kind.values())
    while len(out) < n and any(queues):
        for q in queues:
            if q and len(out) < n:
                out.append(q.pop(0))
    return out


def lot_posts(con, state_dir, today, policy=POLICY) -> list[Post]:
    return [Post(f"lot:{r['kind']}:{r['address']}:{r['end_d']}", "lot", templates.lot(r), templates.lot_card(r),
                 score=r["discount"], facts=facts.lot(r)) for r in lots.new_deals(con, state_dir, today, policy["lot_min_discount"])]


def drop_posts(con, state_dir, today, policy=POLICY) -> list[Post]:
    return [Post(f"drop:{r['record_id']}:{r['price']:.0f}", "drop", templates.drop(r), templates.drop_card(r),
                 score=r["drop"], facts=facts.drop(r)) for r in lots.price_drops(con, state_dir, today, policy["drop_min"])]


def house_posts(con, state_dir, today, policy=POLICY) -> list[Post]:
    return [Post(f"house:{r['kind']}:{r['address']}:{r['end_d']}", "house", templates.house(r), templates.house_card(r),
                 score=r["n"], facts=facts.house(r)) for r in lots.new_houses(con, state_dir, today, policy["house_min_lots"])]


def _recent(results: list[dict], today: date, days: int) -> list[dict]:
    since = (today - timedelta(days=days)).isoformat()
    return [r for r in results if (r.get("checked_at") or "")[:10] >= since]


def sold_posts(results: list[dict], today: date, policy=POLICY) -> list[Post]:
    rows = [r for r in _recent(results, today, policy["results_days"])
            if r.get("final_price") and (r.get("final_to_start") or 0) >= policy["sold_min_ratio"]]
    rows.sort(key=lambda r: -r["final_to_start"])
    seen, out = set(), []
    for r in rows:   # из одного дома — только самый яркий итог: соседние лоты похожи
        if r.get("address") not in seen:
            seen.add(r.get("address"))
            out.append(Post(f"result:{r['tender_id']}", "sold", templates.sold(r), templates.sold_card(r),
                            score=r["final_to_start"], facts=facts.sold(r)))
    return out


def nofight_posts(results: list[dict], today: date, policy=POLICY) -> list[Post]:
    rows = [r for r in _recent(results, today, policy["results_days"])
            if r.get("status") == "Единственный участник" and r.get("final_price")]
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["kind"], r.get("address"), r.get("trades_date")), []).append(r)
    out = []
    for g in sorted(groups.values(), key=lambda g: -max(r["final_price"] for r in g)):
        top = max(g, key=lambda r: r["final_price"])
        out.append(Post(f"nofight:{top['tender_id']}", "nofight", templates.nofight(top, len(g) - 1),
                        templates.nofight_card(top, len(g) - 1), score=top["final_price"],
                        facts=facts.nofight(top, len(g) - 1)))
    return out


def week_posts(con, today: date, policy=POLICY) -> list[Post]:
    y, w, _ = today.isocalendar()
    return [Post(f"week:{kind}:{y}-W{w:02d}", "week", templates.week(kind, ls, today),
                 templates.week_card(kind, ls, today), score=1.0, facts=facts.week(kind, ls, today))
            for kind, ls in lots.week_ahead(con, today, policy["week_min_discount"], policy["week_lots"]).items()
            if len(ls) >= policy["week_min_lots"]]


def weekstats_posts(results: list[dict], today: date, policy=POLICY) -> list[Post]:
    start = today - timedelta(days=6)
    y, w, _ = today.isocalendar()
    out = []
    for kind in lots.KINDS:
        rows = [r for r in results if r["kind"] == kind and (d := lots.trades_date(r)) and start <= d <= today]
        if len(rows) < policy["weekstats_min"]:
            continue
        sold = [r for r in rows if r.get("final_price")]
        st = {
            "period": f"{start.day}–{templates.day(today)}" if start.month == today.month
            else f"{templates.day(start)} – {templates.day(today)}",
            "total": len(rows), "sold": len(sold),
            "at_start": sum(1 for r in sold if (r.get("final_to_start") or 0) <= 1.005),
            "record": max(sold, key=lambda r: r.get("final_to_start") or 0) if sold else None,
            "failed": sum(1 for r in rows if r.get("status") == "Признаны несостоявшимися"),
            "cancelled": sum(1 for r in rows if r.get("status") == "Отменены"),
            "districts": sorted({r.get("district") for r in sold if r.get("district")}),
        }
        if not st["sold"]:
            continue
        out.append(Post(f"weekstats:{kind}:{y}-W{w:02d}", "weekstats", templates.weekstats(kind, st),
                        templates.weekstats_card(kind, st), score=st["sold"], facts=facts.weekstats(kind, st)))
    return out


def default_rubrics(today: date, policy=POLICY) -> tuple[str, ...]:
    r = ["lot", "drop", "house", "sold", "nofight"]
    if today.weekday() == policy["week_weekday"]:
        r.append("week")
    if today.weekday() == policy["weekstats_weekday"]:
        r.append("weekstats")
    return tuple(r)


def drafts(con, state_dir, today: date, *, results_path=None, rubrics=None, registry=None,
           policy=POLICY) -> list[Post]:
    """Черновики на сегодня без тех, что уже уходили (registry — {ключ: дата})."""
    rubrics = rubrics or default_rubrics(today, policy)
    registry = registry or {}
    have_lots = lots.load(con, state_dir, today)
    results = lots.results(results_path)
    fresh = lambda posts: digest.unsent(posts, registry)   # noqa: E731
    out = []
    if have_lots and "week" in rubrics:
        out += fresh(week_posts(con, today, policy))
    if "weekstats" in rubrics:
        out += fresh(weekstats_posts(results, today, policy))
    if have_lots and "lot" in rubrics:
        posts = fresh(lot_posts(con, state_dir, today, policy))
        out += _mix_posts(posts, policy["lots_per_day"])
    if have_lots and "house" in rubrics:
        out += fresh(house_posts(con, state_dir, today, policy))[:policy["houses_per_day"]]
    if have_lots and "drop" in rubrics:
        out += _mix_posts(fresh(drop_posts(con, state_dir, today, policy)), policy["drops_per_day"])
    if "sold" in rubrics:
        out += _mix_posts(fresh(sold_posts(results, today, policy)), policy["sold_per_day"])
    if "nofight" in rubrics:
        out += _mix_posts(fresh(nofight_posts(results, today, policy)), policy["nofight_per_day"])
    return out


def _mix_posts(posts: list[Post], n: int) -> list[Post]:
    keyed = [{"kind": (p.card or {}).get("type") or p.key.split(":")[1], "post": p} for p in posts]
    return [x["post"] for x in _mix(keyed, n)]
