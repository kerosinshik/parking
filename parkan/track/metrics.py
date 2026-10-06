"""Метрики трёх потоков. Источник — журнал событий (текущее состояние) и даты внутри записей.

История до начала учёта восстанавливается по датам в записях (выдача лицензии, начало
приёма заявок, регистрация аварии). Изменения статусов видны с первой синхронизации.
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

from ..geo import NearestIndex
from ..report import hour_price
from .store import current_sql, events_source

D = "try_strptime(json_extract_string(payload, '$.{f}'), '%d.%m.%Y')::DATE"


def _j(field: str) -> str:
    return f"json_extract_string(payload, '$.{field}')"


def _d(field: str) -> str:
    return D.format(f=field)


def _view(con, state_dir, ds_id: int, name: str) -> bool:
    sql = current_sql(state_dir, ds_id)
    if sql is None:
        return False
    con.execute(f"CREATE OR REPLACE TEMP VIEW {name} AS {sql}")
    return True


def _weeks(today: date, n: int) -> list[date]:
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=n - 1)
    return [start + timedelta(weeks=i) for i in range(n)]


def _series(rows, keys: list[date]) -> list:
    m = {r[0]: r[1] for r in rows}
    return [m.get(k, 0) for k in keys]


# ---------------------------------------------------------------- лицензии (№ 586)

def licenses(con, state_dir, today: date) -> dict | None:
    if not _view(con, state_dir, 586, "lic_raw"):
        return None
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE lic AS
        SELECT record_id, {_j('ObjectName')} AS name, {_j('SubjectName')} AS subject, {_j('INN')} AS inn,
               {_j('District')} AS district, {_j('Address')} AS address, {_j('JobType')} AS job,
               {_j('CurrentLicenseState')} AS state, {_d('LicenseBegin')} AS begin_d,
               {_d('InstallDateOfCurrentLicenseState')} AS state_d,
               coalesce({_j('INN')}, '') || '|' || lower(regexp_replace({_j('Address')}, '\\s+', ' ', 'g')) AS point
        FROM lic_raw
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE lic_points AS
        SELECT point, any_value(name) AS name, any_value(subject) AS subject, any_value(district) AS district,
               any_value(address) AS address,
               CASE WHEN bool_or(job = 'РПО') THEN 'РПО' ELSE 'РПА' END AS job,
               min(begin_d) AS opened,
               bool_or(state = 'действующая') AS active,
               CASE WHEN NOT bool_or(state = 'действующая') THEN max(state_d) END AS closed
        FROM lic GROUP BY point
    """)
    weeks = _weeks(today, 52)
    w0 = weeks[0]
    series = {}
    for job in ("РПО", "РПА"):
        opened = con.execute("SELECT date_trunc('week', opened)::DATE, count(*) FROM lic_points "
                             "WHERE job = ? AND opened >= ? GROUP BY 1", [job, w0]).fetchall()
        closed = con.execute("SELECT date_trunc('week', closed)::DATE, count(*) FROM lic_points "
                             "WHERE job = ? AND closed >= ? GROUP BY 1", [job, w0]).fetchall()
        series[job] = {"opened": _series(opened, weeks), "closed": _series(closed, weeks)}
    d30, d90 = today - timedelta(days=30), today - timedelta(days=90)
    tiles = {}
    for job in ("РПО", "РПА"):
        tiles[job] = dict(zip(("active", "opened30", "closed30"), con.execute(
            "SELECT count(*) FILTER (WHERE active), count(*) FILTER (WHERE opened >= ?), "
            "count(*) FILTER (WHERE closed >= ?) FROM lic_points WHERE job = ?", [d30, d30, job]).fetchone()))
    districts = [dict(zip(("district", "opened", "closed", "net", "active"), r)) for r in con.execute("""
        SELECT district, count(*) FILTER (WHERE opened >= ?), count(*) FILTER (WHERE closed >= ?),
               count(*) FILTER (WHERE opened >= ?) - count(*) FILTER (WHERE closed >= ?),
               count(*) FILTER (WHERE active)
        FROM lic_points WHERE district IS NOT NULL GROUP BY 1 ORDER BY 4 DESC
    """, [d90, d90, d90, d90]).fetchall()]
    chains = [dict(zip(("subject", "opened", "closed", "active"), r)) for r in con.execute("""
        SELECT subject, count(*) FILTER (WHERE opened >= ?), count(*) FILTER (WHERE closed >= ?),
               count(*) FILTER (WHERE active)
        FROM lic_points GROUP BY 1
        HAVING count(*) FILTER (WHERE opened >= ?) + count(*) FILTER (WHERE closed >= ?) > 0
        ORDER BY 2 + 3 DESC LIMIT 25
    """, [d90, d90, d90, d90]).fetchall()]
    chains.sort(key=lambda c: -(c["opened"] + c["closed"]))
    recent = [dict(zip(("date", "kind", "name", "subject", "district", "job"), r)) for r in con.execute("""
        SELECT * FROM (
            SELECT opened, 'открытие', name, subject, district, job FROM lic_points WHERE opened >= ?
            UNION ALL
            SELECT closed, 'закрытие', name, subject, district, job FROM lic_points WHERE closed >= ?
        ) ORDER BY 1 DESC LIMIT 60
    """, [today - timedelta(days=14), today - timedelta(days=14)]).fetchall()]
    return {"weeks": [w.isoformat() for w in weeks], "series": series, "tiles": tiles,
            "districts": districts, "chains": chains[:20], "recent": recent,
            "records": con.execute("SELECT count(*) FROM lic").fetchone()[0],
            "points": con.execute("SELECT count(*) FROM lic_points").fetchone()[0]}


# ---------------------------------------------------------------- машино-места (№ 1461)

def _centroid(geometry: str | None):
    if not geometry:
        return None
    g = json.loads(geometry)
    c = g.get("coordinates")
    pts = []

    def walk(x):
        if isinstance(x, list) and x and isinstance(x[0], (int, float)):
            pts.append(x)
        elif isinstance(x, list):
            for y in x:
                walk(y)
    walk(c)
    if not pts:
        return None
    return sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts)


def street_tariffs(parking_con) -> NearestIndex | None:
    """Индекс уличных тарифов по набору № 623 (если база справочника доступна)."""
    if parking_con is None:
        return None
    rows = parking_con.execute("SELECT latitude, longitude, price FROM current_segments "
                               "WHERE latitude IS NOT NULL").fetchall()
    pts = [(hour_price(p), lat, lon) for lat, lon, p in rows]
    return NearestIndex([(p, lat, lon) for p, lat, lon in pts if p is not None])


def parking_lots(con, state_dir, today: date, parking_con=None) -> dict | None:
    if not _view(con, state_dir, 1461, "lots_raw"):
        return None
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE lots AS
        SELECT record_id, {_j('Address')} AS address, {_j('District')} AS district, {_j('AdmArea')} AS adm,
               try_cast({_j('Space')} AS DOUBLE) AS space, try_cast({_j('StartPrice')} AS DOUBLE) AS price,
               {_j('Stage')} AS stage, {_d('StartReceptionDate')} AS start_d, {_d('EndReceptionDate')} AS end_d,
               {_d('TradesDate')} AS trades_d, geometry, first_seen, last_change
        FROM lots_raw WHERE {_j('ObjectType')} = 'машино-место'
    """)
    idx = street_tariffs(parking_con)
    tariff = {}
    if idx is not None:
        for rid, geom in con.execute("SELECT record_id, geometry FROM lots WHERE geometry IS NOT NULL").fetchall():
            c = _centroid(geom)
            hit = idx.nearest(c[0], c[1], 300) if c else None
            if hit:
                tariff[rid] = hit[0]
    weeks = _weeks(today, 52)
    new = con.execute("SELECT date_trunc('week', start_d)::DATE, count(*) FROM lots WHERE start_d >= ? GROUP BY 1",
                      [weeks[0]]).fetchall()
    med = con.execute("SELECT date_trunc('week', start_d)::DATE, median(price) FROM lots "
                      "WHERE start_d >= ? AND price > 0 GROUP BY 1", [weeks[0]]).fetchall()
    on_sale = "stage = 'опубликовано' AND (end_d IS NULL OR end_d >= ?)"
    t = con.execute(f"""
        SELECT count(*) FILTER (WHERE {on_sale}),
               count(*) FILTER (WHERE start_d >= ?),
               median(price) FILTER (WHERE {on_sale} AND price > 0),
               median(price / nullif(space, 0)) FILTER (WHERE {on_sale} AND price > 0),
               count(*)
        FROM lots
    """, [today, today - timedelta(days=30), today, today]).fetchone()
    districts = []
    for d, n, mp, mpm, ids in con.execute(f"""
        SELECT coalesce(district, '—'), count(*), median(price), median(price / nullif(space, 0)), list(record_id)
        FROM lots WHERE {on_sale} AND price > 0 GROUP BY 1 ORDER BY 2 DESC
    """, [today]).fetchall():
        ts = sorted(tariff[i] for i in ids if i in tariff)
        mt = ts[len(ts) // 2] if ts else None
        districts.append({"district": d, "on_sale": n, "median_price": mp, "median_m2": mpm,
                          "street_tariff": mt, "tariff_coverage": len(ts) / n if n else 0})
    upcoming = [dict(zip(("address", "district", "price", "space", "end", "trades"), r)) for r in con.execute(f"""
        SELECT address, district, price, space, strftime(end_d, '%d.%m.%Y'), strftime(trades_d, '%d.%m.%Y')
        FROM lots WHERE {on_sale} ORDER BY end_d, price LIMIT 40
    """, [today]).fetchall()]
    stage_changes = 0
    src = events_source(state_dir, 1461)
    if src:
        stage_changes = con.execute(f"SELECT count(*) FROM {src} WHERE event = 'changed' "
                                    "AND changed_fields LIKE '%Stage%'").fetchone()[0]
    return {"weeks": [w.isoformat() for w in weeks], "new": _series(new, weeks),
            "median_price": [None if v == 0 else v for v in _series(med, weeks)],
            "tiles": dict(zip(("on_sale", "new30", "median_price", "median_m2", "total"), t)),
            "districts": districts, "upcoming": upcoming, "stage_changes": stage_changes,
            "tariff_linked": bool(tariff)}


# ---------------------------------------------------------------- помехи по адресу (№ 62461, № 62501)

NET_GROUPS = (
    ("Электросети", "%электросет%"),
    ("Теплосети", "%тепл%"),
)


def parking_affected(con, state_dir, parking_con, today: date) -> dict | None:
    """Платные парковочные места, на которые сегодня приходятся работы (строгая оценка).

    Аварийные работы — все действующие; земляные — только с земляными работами,
    сроком до 120 дней и площадью до 2 га (длительные ограждения благоустройства не учитываются).
    """
    if parking_con is None:
        return None
    try:
        from shapely.geometry import shape
        from shapely.ops import transform
        from shapely.strtree import STRtree
    except ImportError:
        return None
    k = math.cos(math.radians(55.75))

    def proj(x, y, z=None):
        return x * k * 111320.0, y * 110540.0

    segs = parking_con.execute("SELECT parking_id, municipality_or_area, capacity_total, geometry "
                               "FROM current_segments WHERE geometry IS NOT NULL").fetchall()
    if not segs:
        return None
    geoms = [transform(proj, shape(json.loads(g))) for *_, g in segs]
    tree = STRtree(geoms)
    hit = set()
    for ds, extra in ((62461, "TRUE"),
                      (62501, f"{_j('WorkType')} LIKE '%1. Земляные работы%' "
                              f"AND {_d('WorkEndDate')} - {_d('WorkStartDate')} <= 120")):
        if not _view(con, state_dir, ds, f"pa{ds}"):
            continue
        for (g,) in con.execute(f"""SELECT geometry FROM pa{ds} WHERE geometry IS NOT NULL
                AND {_d('WorkStartDate')} <= ? AND {_d('WorkEndDate')} >= ? AND {extra}""", [today, today]).fetchall():
            wg = transform(proj, shape(json.loads(g)))
            if ds == 62501 and wg.area > 20000:
                continue
            b = wg.buffer(10)
            hit |= {i for i in tree.query(b) if geoms[i].intersects(b)}
    by = {}
    for i in hit:
        by[segs[i][1]] = by.get(segs[i][1], 0) + (segs[i][2] or 0)
    return {"segments": len(hit), "spaces": sum(segs[i][2] or 0 for i in hit),
            "total_spaces": sum(s[2] or 0 for s in segs),
            "by_district": sorted(by.items(), key=lambda x: -x[1])[:15]}


def works(con, state_dir, today: date, parking_con=None) -> dict | None:
    has_em = _view(con, state_dir, 62461, "em_raw")
    has_ew = _view(con, state_dir, 62501, "ew_raw")
    if not (has_em or has_ew):
        return None
    days = [today - timedelta(days=i) for i in range(89, -1, -1)]
    out = {"days": [d.isoformat() for d in days]}
    if has_em:
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE em AS
            SELECT record_id, {_d('EmCallDate')} AS reg_d, {_d('WorkStartDate')} AS start_d,
                   {_d('WorkEndDate')} AS end_d, {_j('EngineeringNetObj')} AS net, {_j('LeadOfWork')} AS lead,
                   {_j('District')} AS district, {_j('SignOfEmergency')} = 'С отключением абонентов' AS outage,
                   {_j('IsCrashSignOfEmergency')} = 'Да' AS crash, {_j('WorkPlaceDescription')} AS place,
                   {_j('EmergencyDescription')} AS descr
            FROM em_raw
        """)
        series = {}
        for label, like in NET_GROUPS:
            series[label] = _series(con.execute("SELECT reg_d, count(*) FROM em WHERE reg_d >= ? AND lower(net) LIKE ? "
                                                "GROUP BY 1", [days[0], like]).fetchall(), days)
        other = " AND ".join(f"lower(coalesce(net, '')) NOT LIKE '{like}'" for _, like in NET_GROUPS)
        series["Прочие сети"] = _series(con.execute(f"SELECT reg_d, count(*) FROM em WHERE reg_d >= ? AND {other} "
                                                    "GROUP BY 1", [days[0]]).fetchall(), days)
        out["em_series"] = series
        out["em_tiles"] = dict(zip(("active", "active_outage", "new7", "new30"), con.execute("""
            SELECT count(*) FILTER (WHERE start_d <= ? AND end_d >= ?),
                   count(*) FILTER (WHERE start_d <= ? AND end_d >= ? AND outage),
                   count(*) FILTER (WHERE reg_d > ?), count(*) FILTER (WHERE reg_d > ?)
            FROM em
        """, [today, today, today, today, today - timedelta(days=7), today - timedelta(days=30)]).fetchone()))
        out["executors"] = [dict(zip(("lead", "n30", "outage_share", "median_days", "active"), r)) for r in con.execute("""
            SELECT lead, count(*) FILTER (WHERE reg_d > ?),
                   avg(outage::INT) FILTER (WHERE reg_d > ?),
                   median(end_d - start_d) FILTER (WHERE reg_d > ?),
                   count(*) FILTER (WHERE start_d <= ? AND end_d >= ?)
            FROM em GROUP BY 1 HAVING count(*) FILTER (WHERE reg_d > ?) > 0 ORDER BY 2 DESC LIMIT 15
        """, [today - timedelta(days=30)] * 3 + [today, today, today - timedelta(days=30)]).fetchall()]
        out["em_districts"] = con.execute("""
            SELECT district, count(*) FILTER (WHERE start_d <= ? AND end_d >= ?) AS act,
                   count(*) FILTER (WHERE reg_d > ?) AS n30
            FROM em WHERE district IS NOT NULL GROUP BY 1 ORDER BY 3 DESC LIMIT 15
        """, [today, today, today - timedelta(days=30)]).fetchall()
        out["em_recent"] = [dict(zip(("date", "district", "net", "lead", "outage", "place"), r)) for r in con.execute("""
            SELECT strftime(reg_d, '%d.%m.%Y'), district, net, lead, outage, place FROM em
            ORDER BY reg_d DESC, record_id DESC LIMIT 40
        """).fetchall()]
    if has_ew:
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE ew AS
            SELECT record_id, {_d('Date')} AS reg_d, {_d('WorkStartDate')} AS start_d, {_d('WorkEndDate')} AS end_d,
                   {_j('WorkType')} LIKE '%1. Земляные работы%' AS earth, {_j('District')} AS district
            FROM ew_raw
        """)
        out["ew_series"] = {
            "Земляные работы": _series(con.execute("SELECT reg_d, count(*) FROM ew WHERE reg_d >= ? AND earth "
                                                   "GROUP BY 1", [days[0]]).fetchall(), days),
            "Только ограждения и объекты": _series(con.execute("SELECT reg_d, count(*) FROM ew WHERE reg_d >= ? "
                                                               "AND NOT earth GROUP BY 1", [days[0]]).fetchall(), days),
        }
        out["ew_tiles"] = dict(zip(("active_earth", "active_all", "new30"), con.execute("""
            SELECT count(*) FILTER (WHERE earth AND start_d <= ? AND end_d >= ?),
                   count(*) FILTER (WHERE start_d <= ? AND end_d >= ?), count(*) FILTER (WHERE reg_d > ?)
            FROM ew
        """, [today, today, today, today, today - timedelta(days=30)]).fetchone()))
    out["parking"] = parking_affected(con, state_dir, parking_con, today)
    return out


def sync_log(con, state_dir, dataset_ids) -> list[dict]:
    out = []
    for ds in dataset_ids:
        src = events_source(state_dir, ds)
        if not src:
            continue
        for at, a, c, r in con.execute(f"""
            SELECT observed_at, count(*) FILTER (WHERE event = 'added'), count(*) FILTER (WHERE event = 'changed'),
                   count(*) FILTER (WHERE event = 'removed')
            FROM {src} GROUP BY 1 ORDER BY 1 DESC LIMIT 30
        """).fetchall():
            out.append({"dataset": ds, "at": at.strftime("%d.%m.%Y %H:%M"), "added": a, "changed": c, "removed": r})
    out.sort(key=lambda x: x["at"][6:10] + x["at"][3:5] + x["at"][:2] + x["at"][11:], reverse=True)
    return out

