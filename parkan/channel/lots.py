"""Лоты для канала из журнала Пульса (набор № 1461): машино-места и квартиры, только Москва и Новая Москва.

Канал считает свою выборку, а не берёт находки Пульса: Пульс следит только за машино-местами,
а канал сравнивает и квартиры. Машино-места сравниваются по цене лота с медианой района,
квартиры — по цене за м² (площади разные). Если в районе меньше 5 лотов того же типа в продаже,
сравнение — с медианой округа; если и там меньше 5, лот не сравнивается.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ..track import torgi
from ..track.digest import _addr, previous_sync
from ..track.store import current_sql, events_source

KINDS = {"parking": "машино-место", "flat": "квартира"}
BY_TYPE = {v: k for k, v in KINDS.items()}
MIN_HOUSES = 3           # других домов в районе/округе, чтобы медиана по домам что-то значила


def _j(f: str) -> str:
    return f"json_extract_string(payload, '$.{f}')"


def _d(f: str) -> str:
    return f"try_strptime({_j(f)}, '%d.%m.%Y')::DATE"


def load(con, state_dir, today: date) -> bool:
    """Временные таблицы ch_lots (все лоты нужных типов в Москве) и ch_sale (в продаже, со сравнением)."""
    sql = current_sql(state_dir, 1461)
    if sql is None:
        return False
    kinds = ", ".join(f"('{t}', '{k}')" for k, t in KINDS.items())
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE ch_lots AS
        WITH k(t, kind) AS (VALUES {kinds})
        SELECT record_id, {_j('ID')} AS lot_id, k.kind, {_j('Address')} AS address, {_j('District')} AS district,
               {_j('AdmArea')} AS adm, try_cast({_j('Space')} AS DOUBLE) AS space,
               try_cast({_j('StartPrice')} AS DOUBLE) AS price, {_j('Stage')} AS stage,
               {_d('StartReceptionDate')} AS start_d, {_d('EndReceptionDate')} AS end_d, {_d('TradesDate')} AS trades_d,
               {_j('WebSite[0].WebSite')} AS site, {_j('Coordinates')} AS coords, first_seen
        FROM ({sql}) JOIN k ON k.t = {_j('ObjectType')}
        WHERE {_j('OnTerritoryOfMoscow')} = 'да'
    """)
    # Медиана района — по домам: сначала медиана каждого дома, потом медиана этих значений по другим домам района
    # (свой дом лота не считается). Иначе один дом с десятком одинаковых лотов становится «медианой района»
    # (15-я Парковая, 48А — 12 из 20 квартир Северного Измайлова, 15.10.2026). Меньше MIN_HOUSES других домов
    # в районе — сравнение с округом по тому же правилу, иначе сравнения нет.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE ch_sale AS
        WITH s AS (
            SELECT *, CASE WHEN kind = 'flat' THEN price / nullif(space, 0) ELSE price END AS base
            FROM ch_lots
            WHERE stage = 'опубликовано' AND (end_d IS NULL OR end_d >= ?) AND price > 0
        ), h AS (
            SELECT kind, district, adm, address, median(base) AS hm
            FROM s WHERE base IS NOT NULL GROUP BY ALL
        ), d AS (
            SELECT s.record_id, median(h.hm) AS d_med, count(*) AS d_houses
            FROM s JOIN h ON h.kind = s.kind AND h.district = s.district AND h.address <> s.address
            GROUP BY s.record_id
        ), o AS (
            SELECT s.record_id, median(h.hm) AS o_med, count(*) AS o_houses
            FROM s JOIN h ON h.kind = s.kind AND h.adm = s.adm AND h.address <> s.address
            GROUP BY s.record_id
        ), m AS (
            SELECT s.*, d.d_med, coalesce(d.d_houses, 0) AS d_houses, o.o_med, coalesce(o.o_houses, 0) AS o_houses,
                   count(*) OVER (PARTITION BY s.kind, s.address) AS at_addr
            FROM s LEFT JOIN d USING (record_id) LEFT JOIN o USING (record_id)
        )
        SELECT *, CASE WHEN d_houses >= {MIN_HOUSES} THEN d_med WHEN o_houses >= {MIN_HOUSES} THEN o_med END AS ref,
               CASE WHEN d_houses >= {MIN_HOUSES} THEN 'района' WHEN o_houses >= {MIN_HOUSES} THEN 'округа' END AS scope,
               CASE WHEN d_houses >= {MIN_HOUSES} THEN d_houses WHEN o_houses >= {MIN_HOUSES} THEN o_houses END AS ref_houses,
               1 - base / CASE WHEN d_houses >= {MIN_HOUSES} THEN d_med WHEN o_houses >= {MIN_HOUSES} THEN o_med END AS discount
        FROM m
    """, [today])
    return True


def _rows(con, sql: str, args=()) -> list[dict]:
    cur = con.execute(sql, list(args))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def lot_url(site: str | None, lot_id) -> str | None:
    if site:
        return site if site.startswith("http") else "https://" + site.lstrip("/")
    return f"https://torgi.mos.ru/tender/{lot_id}" if lot_id else None


def _finish(rows: list[dict]) -> list[dict]:
    for r in rows:
        r["place"] = _addr(r.get("address"))
        r["url"] = lot_url(r.pop("site", None), r.get("lot_id"))
    return rows


def _new_cond(state_dir, today: date) -> tuple[str, object]:
    """Новые лоты: появились в журнале после прошлой синхронизации; при первой — начало приёма заявок вчера-сегодня."""
    since = previous_sync(state_dir, [1461])
    return ("first_seen > ?", since) if since else ("start_d >= ?", today - timedelta(days=1))


def new_deals(con, state_dir, today: date, min_discount: float) -> list[dict]:
    """Новые лоты дешевле медианы на min_discount и больше; по адресу — один, самый выгодный."""
    cond, arg = _new_cond(state_dir, today)
    return _finish(_rows(con, f"""
        SELECT * EXCLUDE (rn) FROM (
            SELECT *, row_number() OVER (PARTITION BY kind, address ORDER BY discount DESC, price) AS rn
            FROM ch_sale WHERE {cond} AND discount >= ?
        ) WHERE rn = 1 ORDER BY discount DESC
    """, [arg, min_discount]))


def new_houses(con, state_dir, today: date, min_lots: dict) -> list[dict]:
    """Пачки новых лотов в одном доме: kind, address, n, price_min/max, space_min/max, ref, scope, end_d."""
    cond, arg = _new_cond(state_dir, today)
    rows = _rows(con, f"""
        SELECT kind, address, any_value(district) AS district, any_value(adm) AS adm, count(*) AS n,
               min(price) AS price_min, max(price) AS price_max, min(space) AS space_min, max(space) AS space_max,
               min(base) AS base_min, any_value(ref) AS ref, any_value(scope) AS scope, min(end_d) AS end_d,
               arg_min(site, price) AS site, arg_min(lot_id, price) AS lot_id
        FROM ch_sale WHERE {cond}
        GROUP BY kind, address
        ORDER BY n DESC
    """, [arg])
    return [r for r in _finish(rows) if r["n"] >= min_lots.get(r["kind"], 10)]


def price_drops(con, state_dir, today: date, min_drop: float) -> list[dict]:
    """Снижение стартовой цены у той же записи (видно только по журналу) после прошлой синхронизации."""
    src, since = events_source(state_dir, 1461), previous_sync(state_dir, [1461])
    if not src or not since:
        return []
    rows = _rows(con, f"""
        WITH ev AS (
            SELECT record_id, observed_at, try_cast({_j('StartPrice')} AS DOUBLE) AS price,
                   lag(try_cast({_j('StartPrice')} AS DOUBLE)) OVER (PARTITION BY record_id ORDER BY observed_at) AS old_price
            FROM {src} WHERE event <> 'removed'
        )
        SELECT s.*, ev.old_price, 1 - ev.price / ev.old_price AS drop
        FROM ev JOIN ch_sale s USING (record_id)
        WHERE ev.observed_at > ? AND ev.old_price > 0 AND ev.price < ev.old_price * (1 - ?)
        ORDER BY drop DESC
    """, [since, min_drop])
    return _finish(rows)


def best_deals(con, today: date, min_discount: float, min_days_left: int = 2) -> list[dict]:
    """Все лоты в продаже дешевле медианы на min_discount и больше (не только новые); по адресу — один.

    Запас для «Лота дня», когда новых выгодных лотов за сутки нет. До конца заявок — хотя бы min_days_left дней."""
    return _finish(_rows(con, """
        SELECT * EXCLUDE (rn) FROM (
            SELECT *, row_number() OVER (PARTITION BY kind, address ORDER BY discount DESC, price) AS rn
            FROM ch_sale WHERE discount >= ? AND end_d >= ?
        ) WHERE rn = 1 ORDER BY discount DESC
    """, [min_discount, today + timedelta(days=min_days_left)]))


def ahead(con, date_from: date, date_to: date, min_discount: float, n: int) -> dict[str, list[dict]]:
    """По типам: лоты дешевле медианы, приём заявок по которым закрывается с date_from по date_to; по адресу — один."""
    out = {}
    for kind in KINDS:
        out[kind] = _finish(_rows(con, """
            SELECT * EXCLUDE (rn) FROM (
                SELECT *, row_number() OVER (PARTITION BY address ORDER BY discount DESC) AS rn
                FROM ch_sale WHERE kind = ? AND end_d BETWEEN ? AND ? AND discount >= ?
            ) WHERE rn = 1 ORDER BY discount DESC LIMIT ?
        """, [kind, date_from, date_to, min_discount, n]))
    return out


def week_ahead(con, today: date, min_discount: float, n: int, days: int = 7) -> dict[str, list[dict]]:
    """По типам: лоты с дедлайном заявок в ближайшие days дней (не сегодня), дешевле медианы; по адресу — один."""
    return ahead(con, today + timedelta(days=1), today + timedelta(days=days), min_discount, n)


def snapshot(con) -> dict[str, dict]:
    """Срез лотов в продаже по типам: число, медиана (квартиры — ₽/м²), доля дешевле медианы на 25 %+, округа."""
    out = {}
    for kind in KINDS:
        metric = "price / space" if kind == "flat" else "price"
        row = _rows(con, f"""
            SELECT count(*) AS n, median({metric}) AS med,
                   count(*) FILTER (WHERE discount >= 0.25) AS cheap,
                   count(DISTINCT address) AS houses
            FROM ch_sale WHERE kind = ? AND (? = 'parking' OR space > 0)
        """, [kind, kind])[0]
        if not row["n"]:
            continue
        row["adm"] = _rows(con, f"""
            SELECT adm, count(*) AS n, median({metric}) AS med FROM ch_sale
            WHERE kind = ? AND adm IS NOT NULL AND (? = 'parking' OR space > 0)
            GROUP BY adm HAVING count(*) >= 5 ORDER BY med
        """, [kind, kind])
        out[kind] = row
    return out


# ---------------------------------------------------------------- итоги торгов (таблица track-torgi)

def _f(x) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except ValueError:
        return None


def results(path) -> list[dict]:
    """Строки таблиц итогов с полем kind; квартиры — только Москва (есть округ).

    path — файл или несколько через запятую (машино-места Пульса и квартиры канала ведутся в разных таблицах)."""
    if not path:
        return []
    rows = {}
    for one in str(path).split(","):
        if one.strip():
            rows.update(torgi.load(one.strip()))
    out = []
    for r in rows.values():
        kind = BY_TYPE.get(r.get("object_type") or torgi.PARKING)
        if kind is None or (kind != "parking" and not r.get("adm_area")):
            continue
        r = dict(r, kind=kind, place=_addr(r.get("address") or r.get("display_address")))
        for k in ("start_price", "final_price", "final_to_start", "space_m2", "final_price_per_m2"):
            r[k] = _f(r.get(k))
        out.append(r)
    return out


def trades_date(r: dict) -> date | None:
    try:
        return datetime.strptime((r.get("trades_date") or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
