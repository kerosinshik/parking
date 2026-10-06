"""Сопоставление нарушений с парковками и occupancy, почасовые агрегаты по районам.

Шаги (раздел 3 исследования):
 1. координата нарушения -> ближайший парковочный сегмент (или явный street_segment_id);
 2. ближайший снимок occupancy того же сегмента в окне ±window минут
    (предпочтение — снимку ДО события);
 3. occupancy_rate = occupied_spaces / total_spaces;
 4. агрегаты по району и часу.

Везде, где сравниваются загрузка и нарушения, результат — корреляция, а не причинность.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .db import bulk_insert
from .geo import NearestIndex


@dataclass
class MartParams:
    window_min: int = 15            # окно поиска снимка occupancy вокруг события
    max_distance_m: float = 100.0   # максимальное расстояние до сегмента
    near_full: float = 0.9          # порог «почти полной» загрузки
    peak_lookback_min: int = 180    # глубина поиска начала эпизода высокой загрузки
    include_rejected: bool = False  # учитывать ли отклонённые сообщения в числе нарушений


MATCH_COLUMNS = {
    "source_system": "VARCHAR", "violation_id": "VARCHAR", "parking_id": "VARCHAR",
    "match_method": "VARCHAR", "distance_m": "DOUBLE", "area": "VARCHAR", "adm_area": "VARCHAR",
}


def match_segments(con, params: MartParams) -> dict:
    segs = con.execute(
        "SELECT parking_id, latitude, longitude, municipality_or_area, administrative_district "
        "FROM current_segments").fetchall()
    seg_info = {s[0]: (s[3], s[4]) for s in segs}
    index = NearestIndex((s[0], s[1], s[2]) for s in segs if s[1] is not None and s[2] is not None)
    rows, stats = [], {"segment_id": 0, "nearest": 0, "none": 0}
    for sys_, vid, seg_id, lat, lon, area, adm in con.execute(
            "SELECT source_system, violation_id, street_segment_id, latitude, longitude, "
            "municipality_or_area, administrative_district FROM violation_events").fetchall():
        pid, method, dist = None, "none", None
        if seg_id and seg_id in seg_info:
            pid, method = seg_id, "segment_id"
        elif lat is not None:
            hit = index.nearest(lat, lon, params.max_distance_m)
            if hit:
                pid, dist, method = hit[0], round(hit[1], 1), "nearest"
        if pid:
            area, adm = seg_info[pid][0] or area, seg_info[pid][1] or adm
        stats[method] += 1
        rows.append((sys_, vid, pid, method, dist, area, adm))
    con.execute("CREATE OR REPLACE TABLE violation_matches (" +
                ", ".join(f"{k} {t}" for k, t in MATCH_COLUMNS.items()) + ")")
    bulk_insert(con, "violation_matches", MATCH_COLUMNS, rows)
    return stats


def join_occupancy(con, params: MartParams) -> None:
    w, lb, thr = int(params.window_min), int(params.peak_lookback_min), float(params.near_full)
    con.execute("""
        CREATE OR REPLACE VIEW occ AS
        SELECT parking_id, measured_at,
               avg(occupied_spaces) AS occupied, avg(total_spaces) AS total
        FROM occupancy_snapshots GROUP BY parking_id, measured_at
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE violation_occupancy AS
        WITH v AS (
            SELECT e.source_system, e.violation_id, e.violation_type, e.status, e.occurred_at,
                   m.parking_id, m.match_method, m.distance_m, m.area, m.adm_area
            FROM violation_events e JOIN violation_matches m USING (source_system, violation_id)
        ),
        cand AS (
            SELECT v.source_system, v.violation_id, o.measured_at, o.occupied, o.total,
                   row_number() OVER (
                       PARTITION BY v.source_system, v.violation_id
                       ORDER BY (o.measured_at > v.occurred_at),
                                abs(epoch(o.measured_at) - epoch(v.occurred_at))) AS rn
            FROM v JOIN occ o ON o.parking_id = v.parking_id
             AND o.measured_at BETWEEN v.occurred_at - INTERVAL {w} MINUTE
                                   AND v.occurred_at + INTERVAL {w} MINUTE
        ),
        recent AS (   -- снимки до события в пределах lookback
            SELECT v.source_system, v.violation_id, o.measured_at,
                   (o.occupied >= {thr} * o.total) AS high
            FROM v JOIN occ o ON o.parking_id = v.parking_id
             AND o.measured_at <= v.occurred_at
             AND o.measured_at >= v.occurred_at - INTERVAL {lb} MINUTE
        ),
        last_low AS (
            SELECT source_system, violation_id, max(measured_at) AS last_low_at
            FROM recent WHERE NOT high GROUP BY ALL
        ),
        peak AS (     -- начало текущего непрерывного эпизода высокой загрузки
            SELECT r.source_system, r.violation_id, min(r.measured_at) AS peak_start_at
            FROM recent r LEFT JOIN last_low l USING (source_system, violation_id)
            WHERE r.high AND (l.last_low_at IS NULL OR r.measured_at > l.last_low_at)
            GROUP BY ALL
        )
        SELECT v.*,
               c.measured_at AS snapshot_at,
               c.occupied AS occupied_spaces,
               c.total AS total_spaces,
               c.occupied / nullif(c.total, 0) AS occupancy_rate,
               p.peak_start_at,
               (epoch(v.occurred_at) - epoch(p.peak_start_at)) / 60.0 AS minutes_since_peak_start
        FROM v
        LEFT JOIN cand c ON c.source_system = v.source_system AND c.violation_id = v.violation_id AND c.rn = 1
        LEFT JOIN peak p ON p.source_system = v.source_system AND p.violation_id = v.violation_id
    """)


def build_hourly(con, params: MartParams) -> None:
    counted = "status <> 'отклонено'" if not params.include_rejected else "TRUE"
    thr = float(params.near_full)
    con.execute(f"""
        CREATE OR REPLACE TABLE hourly_area_metrics AS
        WITH seg_area AS (
            SELECT municipality_or_area AS area,
                   any_value(administrative_district) AS adm_area,
                   count(*) AS n_segments,
                   sum(capacity_total) AS capacity_total,
                   sum(edge_length_m) / 1000.0 AS edge_km
            FROM current_segments WHERE municipality_or_area IS NOT NULL GROUP BY 1
        ),
        occ_parking_h AS (
            SELECT o.parking_id, date_trunc('hour', o.measured_at) AS hour_start,
                   avg(o.occupied) AS occupied, avg(o.total) AS total
            FROM occ o GROUP BY ALL
        ),
        occ_h AS (
            SELECT s.municipality_or_area AS area, p.hour_start,
                   count(*) AS parkings_observed,
                   sum(p.occupied) AS occupied_avg_sum,
                   sum(p.total) AS total_observed,
                   sum(p.occupied) / nullif(sum(p.total), 0) AS occupancy_rate
            FROM occ_parking_h p JOIN current_segments s USING (parking_id)
            WHERE s.municipality_or_area IS NOT NULL
            GROUP BY ALL
        ),
        viol_h AS (
            SELECT area, date_trunc('hour', occurred_at) AS hour_start,
                   count(*) FILTER (WHERE {counted}) AS violations,
                   count(*) FILTER (WHERE status = 'сообщение') AS n_reports,
                   count(*) FILTER (WHERE status = 'проверено') AS n_verified,
                   count(*) FILTER (WHERE status = 'постановление') AS n_rulings,
                   count(*) FILTER (WHERE status = 'отклонено') AS n_rejected,
                   count(*) FILTER (WHERE {counted} AND occupancy_rate IS NOT NULL) AS violations_with_occupancy,
                   avg(CASE WHEN occupancy_rate >= {thr} THEN 1.0 ELSE 0.0 END)
                       FILTER (WHERE {counted} AND occupancy_rate IS NOT NULL) AS near_full_share
            FROM violation_occupancy WHERE area IS NOT NULL GROUP BY ALL
        ),
        hours AS (
            SELECT area, hour_start FROM occ_h UNION SELECT area, hour_start FROM viol_h
        )
        SELECT h.area, sa.adm_area, h.hour_start,
               dayofweek(h.hour_start) AS dow,          -- 0 = воскресенье
               hour(h.hour_start) AS hour,
               (o.hour_start IS NOT NULL) AS occupancy_observed,
               coalesce(v.violations, 0) AS violations,
               coalesce(v.n_reports, 0) AS n_reports,
               coalesce(v.n_verified, 0) AS n_verified,
               coalesce(v.n_rulings, 0) AS n_rulings,
               coalesce(v.n_rejected, 0) AS n_rejected,
               o.occupancy_rate, o.parkings_observed, o.occupied_avg_sum,
               sa.capacity_total, sa.edge_km,
               coalesce(v.violations, 0) * 100.0 / nullif(sa.capacity_total, 0) AS violations_per_100_spaces,
               CASE WHEN o.hour_start IS NOT NULL
                    THEN coalesce(v.violations, 0) * 100.0 / nullif(o.occupied_avg_sum, 0) END
                    AS violations_per_100_occupied_spaces,
               coalesce(v.violations, 0) / nullif(sa.edge_km, 0) AS violations_per_km_of_parking_edge,
               v.near_full_share AS share_of_violations_near_full_occupancy,
               wx.precipitation_mm, wx.temperature_c,
               (SELECT count(*) FROM road_events r
                 WHERE r.starts_at < h.hour_start + INTERVAL 1 HOUR
                   AND (r.ends_at IS NULL OR r.ends_at > h.hour_start)
                   AND (r.municipality_or_area IS NULL OR r.municipality_or_area = h.area)
               ) AS road_events_active
        FROM hours h
        LEFT JOIN occ_h o USING (area, hour_start)
        LEFT JOIN viol_h v USING (area, hour_start)
        LEFT JOIN seg_area sa ON sa.area = h.area
        LEFT JOIN weather_hourly wx ON wx.hour_start = h.hour_start
        ORDER BY h.area, h.hour_start
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE hourly_area_type_metrics AS
        SELECT area, date_trunc('hour', occurred_at) AS hour_start, violation_type, status, count(*) AS n
        FROM violation_occupancy WHERE area IS NOT NULL GROUP BY ALL ORDER BY ALL
    """)


def build_mart(con, params: MartParams | None = None) -> dict:
    params = params or MartParams()
    stats = match_segments(con, params)
    join_occupancy(con, params)
    build_hourly(con, params)
    with_occ = con.execute(
        "SELECT count(*) FROM violation_occupancy WHERE occupancy_rate IS NOT NULL").fetchone()[0]
    hours = con.execute("SELECT count(*) FROM hourly_area_metrics").fetchone()[0]
    return {"matched_by_segment_id": stats["segment_id"], "matched_nearest": stats["nearest"],
            "unmatched": stats["none"], "with_occupancy": with_occ, "hourly_rows": hours}


def pearson(xs, ys) -> float | None:
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 3:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxx = sum((p[0] - mx) ** 2 for p in pairs)
    syy = sum((p[1] - my) ** 2 for p in pairs)
    if sxx == 0 or syy == 0:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in pairs) / math.sqrt(sxx * syy)


def area_summary(con, params: MartParams | None = None, area: str | None = None) -> list[dict]:
    """Сводка по районам за весь период. Нормировки «в сутки» делают районы сопоставимыми."""
    params = params or MartParams()
    counted = "status <> 'отклонено'" if not params.include_rejected else "TRUE"
    where = "WHERE area = ?" if area else ""
    args = [area] if area else []
    rows = con.execute(f"""
        WITH v AS (SELECT * FROM violation_occupancy WHERE area IS NOT NULL AND {counted}),
        h AS (SELECT area, any_value(capacity_total) AS capacity_total, any_value(edge_km) AS edge_km,
                     avg(occupancy_rate) AS mean_occupancy_rate,
                     count(DISTINCT CAST(hour_start AS DATE)) AS days
              FROM hourly_area_metrics GROUP BY area)
        SELECT h.area, h.capacity_total, h.edge_km, h.mean_occupancy_rate, h.days,
               count(v.violation_id) AS violations,
               count(v.violation_id) FILTER (WHERE v.parking_id IS NOT NULL) AS matched,
               count(v.violation_id) FILTER (WHERE v.occupancy_rate IS NOT NULL) AS with_occupancy,
               avg(CASE WHEN v.occupancy_rate >= {float(params.near_full)} THEN 1.0 ELSE 0.0 END)
                   FILTER (WHERE v.occupancy_rate IS NOT NULL) AS near_full_share,
               median(v.minutes_since_peak_start) AS median_minutes_from_peak_start
        FROM h LEFT JOIN v USING (area)
        {where.replace('area', 'h.area')}
        GROUP BY ALL ORDER BY violations DESC
    """, args).fetchall()
    cols = ["area", "capacity_total", "edge_km", "mean_occupancy_rate", "days", "violations",
            "matched", "with_occupancy", "near_full_share", "median_minutes_from_peak_start"]
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        series = con.execute(
            "SELECT occupancy_rate, violations, precipitation_mm FROM hourly_area_metrics "
            "WHERE area = ? AND occupancy_observed", [d["area"]]).fetchall()
        d["corr_occupancy_violations"] = pearson([s[0] for s in series], [s[1] for s in series])
        d["corr_precipitation_violations"] = pearson([s[2] for s in series], [s[1] for s in series])
        days = d["days"] or 1
        d["violations_per_day"] = d["violations"] / days
        d["violations_per_100_spaces_per_day"] = (
            d["violations"] * 100.0 / d["capacity_total"] / days if d["capacity_total"] else None)
        d["violations_per_km_per_day"] = (d["violations"] / d["edge_km"] / days if d["edge_km"] else None)
        out.append(d)
    return out


EXPORT_TABLES = ("current_segments", "parking_changes", "occupancy_snapshots", "violation_events",
                 "road_events", "weather_hourly", "violation_matches", "violation_occupancy",
                 "hourly_area_metrics", "hourly_area_type_metrics")


def export_parquet(con, out_dir) -> list[str]:
    from pathlib import Path
    out = Path(out_dir)
    (out / "normalized").mkdir(parents=True, exist_ok=True)
    (out / "mart").mkdir(parents=True, exist_ok=True)
    existing = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables").fetchall()}
    written = []
    for t in EXPORT_TABLES:
        if t not in existing:
            continue
        sub = "mart" if t.startswith(("hourly", "violation_matches", "violation_occupancy")) else "normalized"
        path = out / sub / f"{t}.parquet"
        con.execute(f"COPY (SELECT * FROM {t}) TO '{path.as_posix()}' (FORMAT parquet)")
        written.append(str(path))
    return written
