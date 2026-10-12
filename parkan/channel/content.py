"""Отбор материалов для канала: что из данных Пульса идёт в черновики сегодня.

Рубрики:
- lot    — «Лот дня»: самые сильные находки по новым лотам (дешевле района, снижение цены);
- result — «Итоги»: проданные лоты с наибольшим ростом цены от стартовой;
- week   — «Неделя вперёд» (по понедельникам): выгодные лоты, приём заявок по которым кончается за 7 дней.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..track import digest, metrics, torgi
from . import templates

# Редакционная политика канала. Отдельно от порогов сводки Trello: их можно менять независимо.
POLICY = {
    "lots_per_day": 2,          # «Лот дня»: не больше стольких лотов в день
    "lot_kinds": ("дешевле района", "снижение цены", "дёшево за м²"),
    "results_per_day": 2,       # «Итоги»: не больше стольких лотов в день
    "results_days": 3,          # итоги, проверенные за последние N дней
    "result_min_ratio": 1.2,    # в эфир — только если продано минимум на 20 % дороже старта
    "week_lots": 7,             # «Неделя вперёд»: сколько лотов в подборке
    "week_min_discount": 0.15,  # и насколько они должны быть дешевле медианы района
    "week_weekday": 0,          # понедельник
}


@dataclass
class Post:
    key: str                    # постоянный ключ: один и тот же материал не попадает в черновики дважды
    rubric: str                 # lot | result | week
    text: str                   # HTML для Telegram (parse_mode=HTML)
    card: dict | None = None    # данные для карточки (cards.render), если она нужна
    repeat_days: int = 0        # совместимость с digest.unsent
    score: float = 0.0
    card_path: object = None    # готовая карточка (заполняет channel-draft)


def lot_posts(con, state_dir, today: date, policy=POLICY) -> list[Post]:
    since = digest.previous_sync(state_dir, [1461])
    found = [f for f in digest.lot_findings(con, state_dir, today, since) if f.kind in policy["lot_kinds"]]
    found.sort(key=lambda f: -f.score)
    return [Post(f.key, "lot", templates.lot(f), card=templates.lot_card(f), score=f.score) for f in found]


def result_posts(results_path, today: date, policy=POLICY) -> list[Post]:
    if not results_path:
        return []
    since = (today - timedelta(days=policy["results_days"])).isoformat()
    rows = []
    for r in torgi.load(results_path).values():
        ratio = _float(r.get("final_to_start"))
        if (r.get("status") in torgi.SOLD and _float(r.get("final_price")) and ratio
                and ratio >= policy["result_min_ratio"] and (r.get("checked_at") or "")[:10] >= since):
            rows.append((ratio, r))
    rows.sort(key=lambda x: -x[0])
    seen, top = set(), []   # из одного дома — только самый яркий итог: соседние лоты похожи
    for ratio, r in rows:
        if r.get("address") not in seen:
            seen.add(r.get("address"))
            top.append((ratio, r))
    return [Post(f"result:{r['tender_id']}", "result", templates.result(r), card=templates.result_card(r), score=ratio)
            for ratio, r in top]


def week_posts(con, state_dir, today: date, policy=POLICY) -> list[Post]:
    m = metrics.parking_lots(con, state_dir, today)
    if m is None:
        return []
    until = today + timedelta(days=7)
    lots = []
    for l in m["lots"]:
        end = _date(l.get("end"))
        if end and today <= end <= until and (l.get("discount") or 0) >= policy["week_min_discount"]:
            lots.append(l)
    lots.sort(key=lambda l: -l["discount"])
    lots = lots[:policy["week_lots"]]
    if not lots:
        return []
    y, w, _ = today.isocalendar()
    return [Post(f"week:{y}-W{w:02d}", "week", templates.week(lots, today), score=1.0)]


def drafts(con, state_dir, today: date, *, results_path=None, rubrics=None, registry=None,
           policy=POLICY) -> list[Post]:
    """Черновики на сегодня без тех, что уже уходили (registry — {ключ: дата})."""
    rubrics = rubrics or (("lot", "result", "week") if today.weekday() == policy["week_weekday"] else ("lot", "result"))
    registry = registry or {}
    out = []
    if "week" in rubrics:
        out += digest.unsent(week_posts(con, state_dir, today, policy), registry)
    if "lot" in rubrics:
        out += digest.unsent(lot_posts(con, state_dir, today, policy), registry)[:policy["lots_per_day"]]
    if "result" in rubrics:
        out += digest.unsent(result_posts(results_path, today, policy), registry)[:policy["results_per_day"]]
    return out


def _float(x) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except ValueError:
        return None


def _date(s: str | None) -> date | None:
    try:
        return datetime.strptime(s, "%d.%m.%Y").date() if s else None
    except ValueError:
        return None
