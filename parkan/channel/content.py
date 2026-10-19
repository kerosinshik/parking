"""Отбор материалов для канала: что из данных Пульса идёт в черновики сегодня.

Типы объектов — машино-места и квартиры (Москва и Новая Москва, parkan/channel/lots.py).
Рубрики (docs/channel-design.md):
- lot      — «Лот дня»: новый лот дешевле медианы района (квартиры — за м²); нет новых — лучший из лотов в продаже,
             который ещё не выходил;
- drop     — «Снижение цены»: город снизил стартовую цену;
- house    — «Целый дом»: пачка новых лотов в одном доме;
- sold     — «Продано дороже»: итог торгов заметно выше старта;
- nofight  — «Без борьбы»: один участник, продано по стартовой цене;
- week     — «Две недели вперёд» (понедельник и четверг): выгодные лоты с дедлайном заявок в ближайшие 14 дней;
- deadline — «Заявки заканчиваются» (каждый день): выгодные лоты, приём заявок по которым закрывается через 3 дня;
- snapshot — «Срез торгов» (понедельник и среда): лоты в продаже, медианы, округа, итоги за 30 дней;
- weekstats — «Итоги недели» (пятница): продано, доля по старту, рекорд;
- torgi    — «Старт ниже итогов»: квартира в продаже, старт за м² заметно ниже итогов торгов в её доме или районе.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..track import digest
from . import facts, lots, market, market_check, templates

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
    "lot_fallback_days_left": 3,
    "address_repeat_days": 30,  # адрес из «Старт ниже итогов», «Лота дня», «Целого дома» не повторяется 30 дней   # «Лот дня» из лотов в продаже: до конца заявок хотя бы 3 дня
    "week_min_discount": 0.15,  # «Две недели вперёд» и «Заявки заканчиваются»
    "week_lots": 7,
    "week_min_lots": 3,         # меньше трёх лотов — подборку не делаем
    "week_weekdays": (0, 3),    # понедельник и четверг
    "week_days": 14,            # горизонт подборки: соседние подборки частично пересекаются — так и задумано
    "deadline_days": 3,         # «Заявки заканчиваются»: лоты, у которых последний день заявок через 3 дня
    "deadline_lots": 5,
    "deadline_min_lots": 2,
    "snapshot_weekdays": (0, 2),   # «Срез торгов»: понедельник и среда
    "snapshot_days": 30,        # итоги в срезе — за 30 дней
    "weekstats_weekday": 4,     # пятница
    "weekstats_min": 3,         # итогов за неделю по типу, чтобы был пост
    # рыночный ориентир старше — не показываем (журнал market-add); Росстат по Москве — раз в квартал
    "market_max_age_days": {"район": 60, "округ": 60, "город": 200},
    "torgi_days": 90,           # ориентир по итогам торгов: квартиры, проданные за 90 дней
    "torgi_min_gap": 0.20,      # «Старт ниже итогов»: старт за м² ниже ориентира минимум на 20 %
    "torgi_levels": ("дом", "район"),   # ориентир по округу слишком грубый для отдельного поста
    "torgi_min_days_left": 2,   # до конца приёма заявок — хотя бы 2 дня, чтобы читатель успел
    "torgi_per_day": 2,         # сравнение с домом и (или) с районом — единичные «золотые» случаи не теряются
}
RUBRICS = ("lot", "drop", "house", "sold", "nofight", "week", "weekstats", "torgi", "deadline", "snapshot")


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
    market_lot: dict | None = None   # лот в продаже со стартом ниже медианы: кнопки «проверить рынок» (market_check)
    address: str | None = None  # адрес лота в продаже: за день — один пост на адрес среди torgi, lot, drop, house


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


def lot_posts(con, state_dir, today, policy=POLICY, obs=(), sold=None, rows=None) -> list[Post]:
    """«Лот дня» из новых лотов; rows — свой набор (запас из лотов в продаже, lots.best_deals)."""
    if rows is None:
        rows = lots.new_deals(con, state_dir, today, policy["lot_min_discount"])
    market.enrich(rows, obs, today, policy["market_max_age_days"], sold=sold)
    return [Post(f"lot:{r['kind']}:{r['address']}:{r['end_d']}", "lot", templates.lot(r), templates.lot_card(r),
                 score=r["discount"], facts=facts.lot(r), market_lot=market_check.lot_for_market(r),
                 address=r["address"]) for r in rows]


def drop_posts(con, state_dir, today, policy=POLICY, obs=(), sold=None) -> list[Post]:
    rows = lots.price_drops(con, state_dir, today, policy["drop_min"])
    market.enrich(rows, obs, today, policy["market_max_age_days"], sold=sold)
    return [Post(f"drop:{r['record_id']}:{r['price']:.0f}", "drop", templates.drop(r), templates.drop_card(r),
                 score=r["drop"], facts=facts.drop(r), market_lot=market_check.lot_for_market(r),
                 address=r["address"]) for r in rows]


def house_posts(con, state_dir, today, policy=POLICY) -> list[Post]:
    return [Post(f"house:{r['kind']}:{r['address']}:{r['end_d']}", "house", templates.house(r), templates.house_card(r),
                 score=r["n"], facts=facts.house(r), address=r["address"])
            for r in lots.new_houses(con, state_dir, today, policy["house_min_lots"])]


def torgi_posts(con, today: date, sold: list[dict], policy=POLICY) -> list[Post]:
    """Квартиры в продаже, у которых старт за м² ниже итогов торгов в том же доме или районе.

    Без пометки «уже не продалась»; по адресу — один лот (самый дешёвый к ориентиру); ключ — адрес, чтобы дом,
    где выставили сразу несколько квартир, не шёл в канал каждый день."""
    if not sold:
        return []
    rows = lots._finish(lots._rows(con, "SELECT * FROM ch_sale WHERE kind = 'flat' AND (end_d IS NULL OR end_d >= ?)",
                                   [today + timedelta(days=policy["torgi_min_days_left"])]))
    best: dict[str, tuple] = {}
    for r in rows:
        ref = market.torgi_reference(sold, r.get("district"), r.get("adm"), address=r.get("address"))
        if not ref or ref["level"] not in policy["torgi_levels"] or not r.get("space"):
            continue
        if market.torgi_relisted(sold, r.get("address"), r.get("space")):
            continue
        gap = 1 - r["price"] / r["space"] / ref["value"]
        if gap >= policy["torgi_min_gap"] and (r["address"] not in best or gap > best[r["address"]][0]):
            best[r["address"]] = (gap, r, ref)
    out = []
    for gap, r, ref in sorted(best.values(), key=lambda x: -x[0]):
        market.enrich([r], [], today, policy["market_max_age_days"], sold=sold)
        out.append(Post(f"torgi:{r['address']}", "torgi", templates.torgi(r, ref), templates.torgi_card(r, ref),
                        score=gap, facts=facts.torgi(r), market_lot=market_check.lot_for_market(r, below=True),
                        address=r["address"]))
    return out


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
    days = policy["week_days"]
    return [Post(f"week:{kind}:{today.isoformat()}", "week", templates.week(kind, ls, today, days),
                 templates.week_card(kind, ls, today, days), score=1.0, facts=facts.week(kind, ls, today, days))
            for kind, ls in lots.week_ahead(con, today, policy["week_min_discount"], policy["week_lots"], days).items()
            if len(ls) >= policy["week_min_lots"]]


def deadline_posts(con, today: date, policy=POLICY) -> list[Post]:
    """Лоты, у которых последний день заявок ровно через deadline_days дней: каждый лот попадает в рубрику один раз."""
    until = today + timedelta(days=policy["deadline_days"])
    return [Post(f"deadline:{kind}:{until.isoformat()}", "deadline", templates.deadline(kind, ls, until),
                 templates.deadline_card(kind, ls, until), score=1.0, facts=facts.deadline(kind, ls, until))
            for kind, ls in lots.ahead(con, until, until, policy["week_min_discount"], policy["deadline_lots"]).items()
            if len(ls) >= policy["deadline_min_lots"]]


def snapshot_posts(con, results: list[dict], today: date, policy=POLICY) -> list[Post]:
    """Срез торгов по типам: лоты в продаже (число, медиана, дешёвые и дорогие округа) и итоги за snapshot_days дней."""
    since = today - timedelta(days=policy["snapshot_days"])
    out = []
    for kind, st in lots.snapshot(con).items():
        st = dict(st, today=today, days=policy["snapshot_days"])
        if len(st["adm"]) >= 2:
            st["adm_low"], st["adm_high"] = st["adm"][0], st["adm"][-1]
        sold = [r for r in results if r["kind"] == kind and r.get("final_price")
                and (d := lots.trades_date(r)) and since <= d <= today]
        if sold:
            ratios = sorted(r.get("final_to_start") or 1 for r in sold)
            st["sold"] = len(sold)
            st["at_start"] = sum(1 for x in ratios if x <= 1.005)
            st["growth"] = ratios[len(ratios) // 2]
        out.append(Post(f"snapshot:{kind}:{today.isoformat()}", "snapshot", templates.snapshot(kind, st),
                        templates.snapshot_card(kind, st), score=st["n"], facts=facts.snapshot(kind, st)))
    return out


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
    r = ["lot", "drop", "house", "sold", "nofight", "torgi", "deadline"]
    if today.weekday() in policy["snapshot_weekdays"]:
        r.append("snapshot")
    if today.weekday() in policy["week_weekdays"]:
        r.append("week")
    if today.weekday() == policy["weekstats_weekday"]:
        r.append("weekstats")
    return tuple(r)


def drafts(con, state_dir, today: date, *, results_path=None, rubrics=None, registry=None,
           policy=POLICY, market_path=None) -> list[Post]:
    """Черновики на сегодня без тех, что уже уходили (registry — {ключ: дата}).

    market_path — журнал рыночных цен (market.py): квартиры получают в фактах сравнение с рынком."""
    rubrics = rubrics or default_rubrics(today, policy)
    registry = registry or {}
    have_lots = lots.load(con, state_dir, today)
    results = lots.results(results_path)
    obs = market.load(market_path)
    sold = market.torgi_sold(results, today, policy["torgi_days"])   # свой ориентир: итоги торгов района
    market.enrich(results, obs, today, policy["market_max_age_days"], price="final_price", space="space_m2",
                  adm="adm_area", sold=sold, own_id="tender_id")
    fresh = lambda posts: digest.unsent(posts, registry)   # noqa: E731
    out = []
    if have_lots and "week" in rubrics:
        out += fresh(week_posts(con, today, policy))
    if "weekstats" in rubrics:
        out += fresh(weekstats_posts(results, today, policy))
    if have_lots and "snapshot" in rubrics:
        out += fresh(snapshot_posts(con, results, today, policy))
    # один адрес — один пост в день: квартира, которая проходит и в «Старт ниже итогов», и в «Лот дня», идёт в первую
    # (ориентир по реальным сделкам), а «Лот дня» берёт следующий лот (решение 15.10.2026)
    used = recent_addresses(registry, today, policy["address_repeat_days"])

    def free(posts: list[Post]) -> list[Post]:
        return [p for p in posts if not p.address or p.address not in used]

    def take(posts: list[Post]) -> list[Post]:
        used.update(p.address for p in posts if p.address)
        return posts

    if have_lots and "torgi" in rubrics:
        out += take(free(fresh(torgi_posts(con, today, sold, policy)))[:policy["torgi_per_day"]])
    if have_lots and "lot" in rubrics:
        posts = _mix_posts(free(fresh(lot_posts(con, state_dir, today, policy, obs, sold))), policy["lots_per_day"])
        # новых выгодных мало — добираем до дневной нормы лучшими из тех, что уже в продаже; уже ушедшие сегодня
        # «лоты дня» считаются, чтобы повторный запуск в тот же день не добавлял новые
        sent_today = sum(1 for k, d in registry.items() if k.startswith("lot:") and str(d)[:10] == today.isoformat())
        room = policy["lots_per_day"] - len(posts) - sent_today
        if room > 0:
            spare = lots.best_deals(con, today, policy["lot_min_discount"], policy["lot_fallback_days_left"])
            taken = {p.key for p in posts}
            spare = [p for p in free(fresh(lot_posts(con, state_dir, today, policy, obs, sold, rows=spare)))
                     if p.key not in taken]
            posts += _mix_posts(spare, room)
        out += take(posts)
    if have_lots and "deadline" in rubrics:
        out += fresh(deadline_posts(con, today, policy))
    if have_lots and "house" in rubrics:
        out += take(free(fresh(house_posts(con, state_dir, today, policy)))[:policy["houses_per_day"]])
    if have_lots and "drop" in rubrics:
        out += take(_mix_posts(free(fresh(drop_posts(con, state_dir, today, policy, obs, sold))), policy["drops_per_day"]))
    if "sold" in rubrics:
        out += _mix_posts(fresh(sold_posts(results, today, policy)), policy["sold_per_day"])
    if "nofight" in rubrics:
        out += _mix_posts(fresh(nofight_posts(results, today, policy)), policy["nofight_per_day"])
    return out


def recent_addresses(registry: dict, today: date, days: int) -> set[str]:
    """Адреса лотов в продаже из постов за последние days дней (torgi, lot, house): в эти рубрики снова не идут."""
    since = (today - timedelta(days=days)).isoformat()
    out = set()
    for key, d in registry.items():
        if str(d)[:10] < since:
            continue
        rubric, _, rest = key.partition(":")
        if rubric == "torgi":
            out.add(rest)
        elif rubric in ("lot", "house") and rest.count(":") >= 2:
            out.add(rest.split(":", 1)[1].rsplit(":", 1)[0])    # kind:адрес:конец заявок
    return out


def _mix_posts(posts: list[Post], n: int) -> list[Post]:
    keyed = [{"kind": (p.card or {}).get("type") or p.key.split(":")[1], "post": p} for p in posts]
    return [x["post"] for x in _mix(keyed, n)]
