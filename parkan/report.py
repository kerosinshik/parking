"""Самодостаточный HTML-дашборд (без внешних зависимостей).

Два слоя:
  инфраструктура — строится по одному справочнику парковок (набор № 623): вся Москва и районы;
  аналитика загрузки и нарушений — появляется для района, когда загружены occupancy и нарушения
  и собрана витрина (build-mart). Без этих данных блоки помечаются как ожидающие источника.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from statistics import median

from .mart import MartParams, area_summary

DOW_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MAX_MAP_POINTS = 4000
NO_AREA = "Без района"
_PRICE_KEY = re.compile(r"price|cost|стоим|tariff|тариф", re.I)
_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _num(v, nd=3):
    return None if v is None else round(float(v), nd)


CAR = "легков"
PEAK_PERIOD = "будни"


def _tariff_hour_price(t: dict) -> float | None:
    """Почасовая цена одного тарифа набора № 623.

    Фиксированный тариф — HourPrice. Дифференцированный — цена последующих часов
    (FollowingHoursPrice / RestOfTheDayPrice), иначе первых часов; цена первых минут
    пересчитывается в час как запасной вариант.
    """
    for k in ("HourPrice", "FollowingHoursPrice", "RestOfTheDayPrice", "FirstHoursPrice"):
        v = t.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    price, minutes = t.get("FirstMinutesPrice"), t.get("FirstMinutesNumber")
    if isinstance(price, (int, float)) and isinstance(minutes, (int, float)) and minutes > 0:
        return float(price) * 60 / minutes
    return None


def hour_price(price_text) -> float | None:
    """Пиковая почасовая цена для легкового автомобиля.

    Для списка тарифов набора № 623 — максимум по будням для легковых (0 — бесплатно);
    для произвольного поля — максимальное число у ключей цены/тарифа.
    """
    if price_text in (None, ""):
        return None
    try:
        data = json.loads(price_text)
    except (TypeError, ValueError):
        data = price_text
    if isinstance(data, list) and data and all(isinstance(t, dict) for t in data) \
            and any("VehicleTypeForThisTariff" in t for t in data):
        cars = [t for t in data if not t.get("is_deleted")
                and CAR in str(t.get("VehicleTypeForThisTariff", "")).lower()]
        peak = [t for t in cars if PEAK_PERIOD in str(t.get("TariffPeriod", "")).lower()] or cars
        prices = [p for p in map(_tariff_hour_price, peak) if p is not None]
        return max(prices) if prices else None
    found: list[float] = []

    def walk(v, keyed=False):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, keyed or bool(_PRICE_KEY.search(str(k))))
        elif isinstance(v, list):
            for x in v:
                walk(x, keyed)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            if keyed:
                found.append(float(v))
        elif isinstance(v, str) and keyed:
            found.extend(float(n.replace(",", ".")) for n in _NUM.findall(v))

    walk(data, keyed=not isinstance(data, (dict, list)))
    found = [f for f in found if 0 < f < 100_000]
    return max(found) if found else None


def _has_table(con, name: str) -> bool:
    return con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
                       [name]).fetchone()[0] > 0


def _round_coords(c):
    if isinstance(c, list):
        if c and isinstance(c[0], (int, float)):
            return [round(c[0], 6), round(c[1], 6)]
        return [_round_coords(x) for x in c]
    return c


def collect_infrastructure(con) -> dict:
    rows = con.execute("""
        SELECT parking_id, coalesce(name, address, parking_id), coalesce(municipality_or_area, ?),
               administrative_district, zone_id, capacity_total, capacity_disabled, edge_length_m,
               latitude, longitude, geometry, price, address
        FROM current_segments ORDER BY 3, 1
    """, [NO_AREA]).fetchall()
    areas: dict[str, dict] = {}
    segments = []
    for (pid, name, area, adm, zone, cap, dis, edge, lat, lon, geom, price, addr) in rows:
        p = hour_price(price)
        a = areas.setdefault(area, {"area": area, "adm_area": adm, "segments": 0, "capacity": 0,
                                    "disabled": 0, "edge_m": 0.0, "zones": set(), "prices": []})
        a["segments"] += 1
        a["capacity"] += cap or 0
        a["disabled"] += dis or 0
        a["edge_m"] += edge or 0
        if zone:
            a["zones"].add(zone)
        if p is not None:
            a["prices"].append(p)
        g = json.loads(geom) if geom else None
        segments.append({"id": pid, "name": name, "addr": addr, "area": area, "zone": zone, "cap": cap,
                         "dis": dis, "price": p, "lat": _num(lat, 6), "lon": _num(lon, 6),
                         "geom": {"type": g["type"], "coordinates": _round_coords(g["coordinates"])}
                         if g and g.get("coordinates") else None})
    out_areas = []
    for a in areas.values():
        out_areas.append({
            "area": a["area"], "adm_area": a["adm_area"], "segments": a["segments"],
            "capacity": a["capacity"], "disabled": a["disabled"], "edge_km": round(a["edge_m"] / 1000, 2),
            "zones": len(a["zones"]),
            "price_median": median(a["prices"]) if a["prices"] else None,
        })
    out_areas.sort(key=lambda x: (-x["capacity"], x["area"]))
    all_prices = [s["price"] for s in segments if s["price"] is not None]
    meta = con.execute("""
        SELECT max(valid_from), any_value(source_version) FILTER (WHERE valid_from = (SELECT max(valid_from) FROM current_segments))
        FROM current_segments
    """).fetchone()
    last_changes = dict(con.execute("""
        SELECT change_type, count(*) FROM parking_changes
        WHERE changed_at = (SELECT max(changed_at) FROM parking_changes) GROUP BY 1
    """).fetchall())
    return {
        "areas": out_areas,
        "segments": segments,
        "city": {"segments": len(segments), "capacity": sum(a["capacity"] for a in out_areas),
                 "disabled": sum(a["disabled"] for a in out_areas),
                 "edge_km": round(sum(a["edge_km"] for a in out_areas), 1),
                 "areas": len([a for a in out_areas if a["area"] != NO_AREA]),
                 "zones": len({s["zone"] for s in segments if s["zone"]}),
                 "price_median": median(all_prices) if all_prices else None},
        "loaded_at": meta[0].strftime("%d.%m.%Y %H:%M") if meta and meta[0] else None,
        "version": meta[1] if meta else None,
        "last_changes": last_changes,
    }


def collect_analytics(con, params: MartParams) -> dict:
    """Аналитика загрузки и нарушений по районам; пусто, если витрина не собрана."""
    if not _has_table(con, "hourly_area_metrics"):
        return {"areas": {}, "period": [None, None], "coverage": None}
    counted = "status <> 'отклонено'" if not params.include_rejected else "TRUE"
    out = {}
    for s in area_summary(con, params):
        a = s["area"]
        prof = con.execute("""
            SELECT hour, (dow IN (0, 6)) AS weekend, avg(occupancy_rate), avg(violations)
            FROM hourly_area_metrics WHERE area = ? AND occupancy_observed GROUP BY ALL
        """, [a]).fetchall()
        profile = {"weekday": {"occ": [None] * 24, "viol": [None] * 24},
                   "weekend": {"occ": [None] * 24, "viol": [None] * 24}}
        for hour, weekend, occ, viol in prof:
            k = "weekend" if weekend else "weekday"
            profile[k]["occ"][hour] = _num(occ)
            profile[k]["viol"][hour] = _num(viol)
        heat = [[0.0] * 24 for _ in range(7)]
        for dow, hour, v in con.execute("""
            SELECT (dow + 6) % 7, hour, avg(violations)
            FROM hourly_area_metrics WHERE area = ? AND occupancy_observed GROUP BY ALL
        """, [a]).fetchall():
            heat[dow][hour] = _num(v)
        scatter = [[_num(r[0]), r[1], r[2].strftime("%d.%m %H:00")] for r in con.execute("""
            SELECT occupancy_rate, violations, hour_start FROM hourly_area_metrics
            WHERE area = ? AND occupancy_observed AND occupancy_rate IS NOT NULL ORDER BY hour_start
        """, [a]).fetchall()]
        seg_stats = {r[0]: {"occ": _num(r[1]), "viol": r[2]} for r in con.execute(f"""
            SELECT s.parking_id,
                   (SELECT sum(occupied_spaces) / nullif(sum(total_spaces), 0)
                      FROM occupancy_snapshots o WHERE o.parking_id = s.parking_id),
                   (SELECT count(*) FROM violation_occupancy v WHERE v.parking_id = s.parking_id AND {counted})
            FROM current_segments s WHERE s.municipality_or_area = ?
        """, [a]).fetchall()}
        points = [[round(r[0], 6), round(r[1], 6)] for r in con.execute(f"""
            SELECT e.latitude, e.longitude FROM violation_occupancy v
            JOIN violation_events e USING (source_system, violation_id)
            WHERE v.area = ? AND e.latitude IS NOT NULL AND {counted.replace('status', 'v.status')}
            LIMIT {MAX_MAP_POINTS}
        """, [a]).fetchall()]
        types = [[t[0]] + [int(x or 0) for x in t[1:]] for t in con.execute("""
            SELECT violation_type,
                   sum(n) FILTER (WHERE status = 'сообщение'),
                   sum(n) FILTER (WHERE status = 'проверено'),
                   sum(n) FILTER (WHERE status = 'постановление'),
                   sum(n) FILTER (WHERE status = 'отклонено'),
                   sum(n)
            FROM hourly_area_type_metrics WHERE area = ? GROUP BY 1 ORDER BY 6 DESC
        """, [a]).fetchall()]
        out[a] = {"summary": {k: (_num(v) if isinstance(v, float) else v) for k, v in s.items()},
                  "profile": profile, "heat": heat, "scatter": scatter, "seg": seg_stats,
                  "points": points, "types": types}
    period = con.execute("SELECT min(hour_start), max(hour_start) FROM hourly_area_metrics").fetchone()
    cov = con.execute("""
        SELECT count(*), count(parking_id), count(occupancy_rate), count(*) FILTER (WHERE area IS NULL)
        FROM violation_occupancy
    """).fetchone()
    return {
        "areas": out,
        "period": [p.strftime("%d.%m.%Y") if p else None for p in period],
        "coverage": {"total": cov[0], "matched": cov[1], "with_occupancy": cov[2], "no_area": cov[3]},
    }


def collect_data(con, params: MartParams | None = None) -> dict:
    params = params or MartParams()
    sources = con.execute("""
        SELECT 'Справочник парковок (набор № 623), версия ' || coalesce(source_version, '?'), count(*)
        FROM current_segments GROUP BY source_version
        UNION ALL
        SELECT 'Загрузка: ' || source_type || ' / ' || source_name, count(*) FROM occupancy_snapshots GROUP BY ALL
        UNION ALL
        SELECT 'Нарушения: ' || source_system, count(*) FROM violation_events GROUP BY ALL
        UNION ALL
        SELECT 'Погода: ' || coalesce(source_name, '?'), count(*) FROM weather_hourly GROUP BY ALL
        UNION ALL
        SELECT 'Дорожные события: ' || coalesce(source_name, '?'), count(*) FROM road_events GROUP BY ALL
    """).fetchall()
    return {
        "infra": collect_infrastructure(con),
        "analytics": collect_analytics(con, params),
        "sources": [[s[0], s[1]] for s in sources],
        "params": {"window_min": params.window_min, "max_distance_m": params.max_distance_m,
                   "near_full": params.near_full, "include_rejected": params.include_rejected},
        "dow": DOW_RU,
    }


def to_fragment(page: str) -> str:
    """Страница без обёртки html/head/body — для хостинга, который добавляет её сам."""
    for tag in ("<!doctype html>\n", '<html lang="ru">\n', "<head>\n", '<meta charset="utf-8">\n',
                '<meta name="viewport" content="width=device-width, initial-scale=1">\n',
                "</head>\n", "<body>\n", "</body>\n", "</html>\n"):
        page = page.replace(tag, "", 1)
    return page


def render(con, out_path, params: MartParams | None = None, title: str = "Парковки Москвы",
           fragment: bool = False) -> Path:
    data = collect_data(con, params)
    payload = json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":")).replace("</", "<\\/")
    page = TEMPLATE.replace("__TITLE__", html.escape(title)).replace("__DATA__", payload)
    if fragment:
        page = to_fragment(page)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


TEMPLATE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface-1: #fcfcfb; --text-primary: #0b0b0b; --text-secondary: #52514e;
  --muted: #898781; --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --series-2: #eb6834; --dot: #2a78d6; --ramp-dir: 1;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface-1: #1a1a19; --text-primary: #ffffff; --text-secondary: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --series-1: #3987e5; --series-2: #d95926; --dot: #3987e5; --ramp-dir: -1;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface-1: #1a1a19; --text-primary: #ffffff; --text-secondary: #c3c2b7;
  --muted: #898781; --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --series-1: #3987e5; --series-2: #d95926; --dot: #3987e5; --ramp-dir: -1;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--text-primary);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1120px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 0 0 4px; }
.sub { color: var(--text-secondary); margin: 0 0 16px; }
.note { color: var(--text-secondary); font-size: 13px; margin: 4px 0 12px; }
.bar { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 16px 0; }
select { font: inherit; padding: 6px 10px; border-radius: 8px; border: 1px solid var(--axis);
  background: var(--surface-1); color: var(--text-primary); max-width: 100%; }
.card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 16px;
  margin-bottom: 16px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 16px; }
.tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; }
.tile .k { color: var(--text-secondary); font-size: 12px; }
.tile .v { font-size: 24px; font-weight: 600; margin-top: 2px; }
.tile .h { color: var(--muted); font-size: 12px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 820px) { .grid2 { grid-template-columns: 1fr; } }
svg { display: block; width: 100%; height: auto; overflow: visible; }
svg text { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; align-items: center; font-size: 12px; color: var(--text-secondary); margin: 8px 0 0; }
.sw { display: inline-block; width: 12px; height: 3px; border-radius: 2px; vertical-align: middle; margin-right: 6px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: right; padding: 6px 8px; border-bottom: 1px solid var(--grid); font-variant-numeric: tabular-nums; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--text-secondary); font-weight: 500; }
th[data-k] { cursor: pointer; user-select: none; }
th[data-k]:hover { color: var(--text-primary); }
tbody tr[data-area] { cursor: pointer; }
tbody tr[data-area]:hover td { background: var(--grid); }
.scroll { overflow-x: auto; }
.tall { max-height: 520px; overflow: auto; }
#tip { position: fixed; pointer-events: none; background: var(--surface-1); color: var(--text-primary);
  border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; font-size: 12px;
  box-shadow: 0 4px 16px rgba(0,0,0,.15); display: none; z-index: 10; max-width: 320px; }
#tip b { font-weight: 600; }
.warn { border-left: 3px solid var(--axis); }
.pending { border-style: dashed; }
.pending ul { margin: 8px 0 0; padding-left: 18px; color: var(--text-secondary); font-size: 13px; }
canvas { display: block; width: 100%; border-radius: 8px; }
[hidden] { display: none !important; }
</style>
</head>
<body>
<main>
  <h1>__TITLE__</h1>
  <p class="sub" id="sub"></p>
  <div class="bar">
    <label for="area">Территория</label>
    <select id="area"></select>
  </div>
  <div class="tiles" id="tiles"></div>

  <div class="card">
    <h2 id="map-title">Карта парковок</h2>
    <p class="note" id="map-note"></p>
    <canvas id="map"></canvas>
    <div id="map-legend"></div>
  </div>

  <div class="card" id="city-block">
    <h2>Районы с наибольшей вместимостью</h2>
    <p class="note">Число парковочных мест по данным справочника. Нажмите на строку таблицы ниже, чтобы открыть район.</p>
    <svg id="top-bars"></svg>
  </div>

  <div class="card" id="areas-card">
    <h2>Районы</h2>
    <div class="scroll tall"><table id="areas"></table></div>
  </div>

  <div class="card pending" id="pending">
    <h2>Загрузка и нарушения — ожидают данных</h2>
    <p class="note" id="pending-text"></p>
    <ul>
      <li>занятость мест (occupancy) — нужен официальный поток или выгрузка ГКУ «АМПП»/ЦОДД, раз в 5–15 минут;</li>
      <li>обезличенные события нарушений — нужна выгрузка МАДИ/ЦОДД/АМПП или «Помощника Москвы».</li>
    </ul>
    <p class="note">Как только данные будут загружены (<code>parkan import-occupancy</code>, <code>parkan import-violations</code>,
      <code>parkan build-mart</code>), здесь появятся профиль дня, тепловая карта нарушений и связь загрузки с нарушениями.</p>
  </div>

  <div id="analytics" hidden>
    <div class="tiles" id="a-tiles"></div>
    <div class="card">
      <h2>Профиль дня</h2>
      <p class="note">Средняя загрузка парковок района и среднее число нарушений за час, отдельно для будней и выходных.</p>
      <div class="legend"><span><i class="sw" style="background:var(--series-1)"></i>Будни</span>
        <span><i class="sw" style="background:var(--series-2)"></i>Выходные</span></div>
      <div class="grid2">
        <div><div class="note">Загрузка, %</div><svg id="prof-occ"></svg></div>
        <div><div class="note">Нарушений за час</div><svg id="prof-viol"></svg></div>
      </div>
    </div>
    <div class="grid2">
      <div class="card">
        <h2>Когда нарушают</h2>
        <p class="note">Среднее число нарушений за час по дням недели.</p>
        <svg id="heat"></svg>
      </div>
      <div class="card">
        <h2>Загрузка и нарушения</h2>
        <p class="note">Каждая точка — один час в районе. Показывает связь, а не причину.</p>
        <svg id="scatter"></svg>
      </div>
    </div>
    <div class="card">
      <h2>Типы нарушений и статусы</h2>
      <div class="scroll"><table id="types"></table></div>
    </div>
  </div>

  <div class="card warn">
    <h2>Источники и ограничения</h2>
    <div id="coverage" class="note"></div>
    <p class="note">Вместимость — это число мест по справочнику, а не число свободных мест сейчас.
      Высокая загрузка и число нарушений могут быть связаны, но это не доказывает, что одно вызывает другое.</p>
    <div class="scroll"><table id="sources"></table></div>
  </div>
</main>
<div id="tip"></div>
<script>
const DATA = __DATA__;
const INFRA = DATA.infra, AN = DATA.analytics;
const RAMP = ["#cde2fb","#b7d3f6","#9ec5f4","#86b6ef","#6da7ec","#5598e7","#3987e5","#2a78d6","#256abf","#1c5cab","#184f95","#104281","#0d366b"];
const NS = "http://www.w3.org/2000/svg";
const CITY = "__city__";
const $ = id => document.getElementById(id);
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const fmt = (v, d = 1) => v == null ? "—" : Number(v).toLocaleString("ru-RU", {maximumFractionDigits: d, minimumFractionDigits: d});
const int = v => v == null ? "—" : Math.round(v).toLocaleString("ru-RU");
const pct = v => v == null ? "—" : fmt(v * 100, 0) + " %";
const rub = v => v == null ? "—" : int(v) + " ₽/ч";
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function rampColor(t) {
  if (t == null || isNaN(t)) return css("--muted");
  t = Math.max(0, Math.min(1, t));
  if (css("--ramp-dir") === "-1") t = 1 - t;
  return RAMP[Math.round(t * (RAMP.length - 1))];
}
// на светлой подложке самые светлые шаги шкалы почти не видны на карте: тонкие линии начинаем с 250-го шага
function mapColor(t) {
  if (t == null || isNaN(t)) return css("--muted");
  return rampColor(0.25 + 0.75 * Math.max(0, Math.min(1, t)));
}
function el(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
function clear(svg, w, h) { svg.innerHTML = ""; svg.setAttribute("viewBox", `0 0 ${w} ${h}`); }
const tip = $("tip");
function showTip(ev, html) {
  tip.innerHTML = html; tip.style.display = "block";
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 14;
  tip.style.left = Math.max(8, x) + "px"; tip.style.top = Math.max(8, y) + "px";
}
function hideTip() { tip.style.display = "none"; }
function niceMax(v) {
  if (!v || v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v))), n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
}
function tickCount(max) {
  const lead = Math.round(max / Math.pow(10, Math.floor(Math.log10(max))));
  return lead === 2 ? 4 : 5;
}
function svgPoint(svg, ev) {
  const pt = svg.createSVGPoint(); pt.x = ev.clientX; pt.y = ev.clientY;
  return pt.matrixTransform(svg.getScreenCTM().inverse());
}
function quantile(sorted, q) {
  if (!sorted.length) return null;
  const i = (sorted.length - 1) * q, lo = Math.floor(i), hi = Math.ceil(i);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (i - lo);
}

/* ---------- карта (canvas: выдерживает тысячи сегментов) ---------- */
function coordsOf(geom) {
  if (!geom) return [];
  const t = geom.type, c = geom.coordinates;
  if (t === "Point") return [[c]];
  if (t === "LineString" || t === "MultiPoint") return [c];
  if (t === "MultiLineString" || t === "Polygon") return c;
  if (t === "MultiPolygon") return c.flat();
  return [];
}
let mapState = null;
function drawMap(segs, {metric, points}) {
  const cv = $("map"), wrap = cv.parentElement;
  const W = wrap.clientWidth - 32 > 0 ? wrap.clientWidth - 32 : 600;
  const H = Math.round(Math.min(620, Math.max(300, W * 0.62)));
  const dpr = window.devicePixelRatio || 1;
  cv.width = W * dpr; cv.height = H * dpr; cv.style.height = H + "px";
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const lines = segs.map(s => { const l = coordsOf(s.geom); return l.length ? l : (s.lat != null ? [[[s.lon, s.lat]]] : []); });
  const pts = lines.flat(2).concat((points || []).map(p => [p[1], p[0]]));
  if (!pts.length) { mapState = null; ctx.fillStyle = css("--muted"); ctx.font = "13px system-ui"; ctx.fillText("Нет координат", 16, 24); return; }
  // устойчивые границы: отбрасываем 0,5 % выбросов по каждой оси
  const xs = pts.map(p => p[0]).sort((a, b) => a - b), ys = pts.map(p => p[1]).sort((a, b) => a - b);
  const q = pts.length > 200 ? 0.005 : 0;
  let x0 = quantile(xs, q), x1 = quantile(xs, 1 - q), y0 = quantile(ys, q), y1 = quantile(ys, 1 - q);
  const k = Math.cos((y0 + y1) / 2 * Math.PI / 180), pad = 16;
  const sx = Math.max((x1 - x0) * k, 1e-4), sy = Math.max(y1 - y0, 1e-4);
  const s = Math.min((W - 2 * pad) / sx, (H - 2 * pad) / sy);
  const ox = (W - sx * s) / 2, oy = (H - sy * s) / 2;
  const px = lon => ox + (lon - x0) * k * s, py = lat => H - oy - (lat - y0) * s;
  const many = segs.length > 1500;
  if (points && points.length) {
    ctx.fillStyle = css("--text-secondary"); ctx.globalAlpha = 0.45;
    points.forEach(p => { ctx.beginPath(); ctx.arc(px(p[1]), py(p[0]), 2.2, 0, 7); ctx.fill(); });
    ctx.globalAlpha = 1;
  }
  ctx.lineCap = "round"; ctx.lineJoin = "round";
  const centers = [];
  segs.forEach((sg, i) => {
    const color = mapColor(metric.value(sg));
    let cx = 0, cy = 0, n = 0;
    lines[i].forEach(line => {
      if (line.length === 1) {
        ctx.fillStyle = color; ctx.beginPath(); ctx.arc(px(line[0][0]), py(line[0][1]), many ? 2 : 4, 0, 7); ctx.fill();
      } else {
        ctx.strokeStyle = color; ctx.lineWidth = many ? 2 : 4; ctx.beginPath();
        line.forEach((c, j) => j ? ctx.lineTo(px(c[0]), py(c[1])) : ctx.moveTo(px(c[0]), py(c[1])));
        ctx.stroke();
      }
      line.forEach(c => { cx += px(c[0]); cy += py(c[1]); n++; });
    });
    if (n) centers.push([cx / n, cy / n, sg]);
  });
  mapState = {centers, metric};
  const lg = $("map-legend");
  lg.innerHTML = `<div class="legend"><span>${esc(metric.label)}: ${esc(metric.lo)}</span>` +
    RAMP.slice(3).map((_, i, a) => `<i style="display:inline-block;width:14px;height:8px;background:${mapColor(i / (a.length - 1))}"></i>`).join("") +
    `<span>${esc(metric.hi)}</span><span><i class="sw" style="background:var(--muted)"></i>нет данных</span>` +
    (points && points.length ? `<span><i class="sw" style="width:6px;height:6px;border-radius:50%;background:var(--text-secondary)"></i>нарушение</span>` : "") + `</div>`;
}
$("map").addEventListener("mousemove", ev => {
  if (!mapState) return;
  const r = $("map").getBoundingClientRect(), x = ev.clientX - r.left, y = ev.clientY - r.top;
  let best = null, bd = 14 * 14;
  for (const c of mapState.centers) { const d = (c[0] - x) ** 2 + (c[1] - y) ** 2; if (d < bd) { bd = d; best = c[2]; } }
  if (!best) { hideTip(); return; }
  showTip(ev, `<b>${esc(best.name)}</b>` + (best.addr && best.addr !== best.name ? `<br>${esc(best.addr)}` : "") +
    `<br>${esc(best.area)}${best.zone ? ", зона " + esc(best.zone) : ""}<br>Мест: ${int(best.cap)}` +
    (best.dis ? `, для инвалидов: ${int(best.dis)}` : "") + `<br>Тариф: ${rub(best.price)}` +
    (best.occ !== undefined ? `<br>Средняя загрузка: ${pct(best.occ)}<br>Нарушений: ${best.viol ?? 0}` : ""));
});
$("map").addEventListener("mouseleave", hideTip);

/* ---------- горизонтальные столбцы ---------- */
function hbars(svg, items, {valueFmt, onClick}) {
  const W = 1000, row = 22, m = {l: 230, r: 80, t: 4};
  const H = m.t + items.length * row + 4;
  clear(svg, W, H);
  const max = Math.max(...items.map(i => i.value), 1), iw = W - m.l - m.r;
  items.forEach((it, i) => {
    const y = m.t + i * row;
    const t = el("text", {x: m.l - 10, y: y + 15, "text-anchor": "end"}, svg);
    t.textContent = it.label.length > 34 ? it.label.slice(0, 33) + "…" : it.label;
    t.style.fill = css("--text-secondary"); t.style.fontSize = "12px";
    const w = Math.max(2, iw * it.value / max);
    el("path", {d: `M${m.l},${y + 4}h${w - 4}a4,4 0 0 1 4,4v6a4,4 0 0 1 -4,4h${-(w - 4)}z`, fill: css("--series-1")}, svg);
    const v = el("text", {x: m.l + w + 6, y: y + 15}, svg); v.textContent = valueFmt(it.value); v.style.fill = css("--text-secondary");
    const hit = el("rect", {x: 0, y, width: W, height: row, fill: "transparent", style: "cursor:pointer"}, svg);
    hit.addEventListener("mousemove", ev => showTip(ev, it.tip));
    hit.addEventListener("mouseleave", hideTip);
    if (onClick) hit.addEventListener("click", () => onClick(it));
  });
}

/* ---------- графики аналитики ---------- */
function lineChart(svg, series, {yMax, yFmt, tipFmt}) {
  const W = 520, H = 220, m = {l: 40, r: 70, t: 10, b: 26};
  clear(svg, W, H);
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const all = series.flatMap(s => s.values).filter(v => v != null);
  const ym = yMax || niceMax(Math.max(...all, 0) * 1.1);
  const x = i => m.l + iw * i / 23, y = v => m.t + ih * (1 - v / ym);
  const nt = yMax ? 4 : tickCount(ym);
  for (let k = 0; k <= nt; k++) {
    const v = ym * k / nt;
    el("line", {x1: m.l, x2: m.l + iw, y1: y(v), y2: y(v), stroke: k ? css("--grid") : css("--axis"), "stroke-width": 1}, svg);
    el("text", {x: m.l - 6, y: y(v) + 4, "text-anchor": "end"}, svg).textContent = yFmt(v);
  }
  for (let h = 0; h < 24; h += 3) el("text", {x: x(h), y: H - 6, "text-anchor": "middle"}, svg).textContent = h + ":00";
  const ends = [];
  series.forEach(s => {
    let d = "", pen = false;
    s.values.forEach((v, i) => { if (v == null) { pen = false; return; } d += (pen ? "L" : "M") + x(i) + "," + y(v); pen = true; });
    el("path", {d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round"}, svg);
    const last = s.values.map((v, i) => [v, i]).filter(p => p[0] != null).pop();
    if (last) ends.push({y: y(last[0]), x: x(last[1]), name: s.name});
  });
  ends.sort((a, b) => a.y - b.y);
  for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 13) ends[i].y = ends[i - 1].y + 13;
  ends.forEach(e => { const t = el("text", {x: e.x + 6, y: e.y + 4}, svg); t.textContent = e.name; t.style.fill = css("--text-secondary"); });
  const cross = el("line", {y1: m.t, y2: m.t + ih, stroke: css("--axis"), "stroke-width": 1, visibility: "hidden"}, svg);
  const dots = series.map(s => el("circle", {r: 4, fill: s.color, stroke: css("--surface-1"), "stroke-width": 2, visibility: "hidden"}, svg));
  const hit = el("rect", {x: m.l, y: m.t, width: iw, height: ih, fill: "transparent"}, svg);
  hit.addEventListener("mousemove", ev => {
    const p = svgPoint(svg, ev), i = Math.max(0, Math.min(23, Math.round((p.x - m.l) / iw * 23)));
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    series.forEach((s, k) => {
      const v = s.values[i];
      if (v == null) { dots[k].setAttribute("visibility", "hidden"); return; }
      dots[k].setAttribute("cx", x(i)); dots[k].setAttribute("cy", y(v)); dots[k].setAttribute("visibility", "visible");
    });
    showTip(ev, `<b>${i}:00–${i + 1}:00</b><br>` + series.map(s =>
      `<i class="sw" style="background:${s.color}"></i>${s.name}: ${tipFmt(s.values[i])}`).join("<br>"));
  });
  hit.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); dots.forEach(d => d.setAttribute("visibility", "hidden")); hideTip(); });
}

function heatmap(svg, heat, dows) {
  const W = 520, cell = 19, gap = 2, m = {l: 28, t: 6, b: 46};
  const H = m.t + 7 * (cell + gap) + m.b;
  clear(svg, W, H);
  const cw = (W - m.l - 4) / 24;
  const max = Math.max(...heat.flat(), 0) || 1;
  heat.forEach((row, d) => {
    el("text", {x: m.l - 6, y: m.t + d * (cell + gap) + cell * 0.7, "text-anchor": "end"}, svg).textContent = dows[d];
    row.forEach((v, h) => {
      const r = el("rect", {x: m.l + h * cw + gap / 2, y: m.t + d * (cell + gap), width: cw - gap, height: cell, rx: 3,
        fill: rampColor(v / max)}, svg);
      r.addEventListener("mousemove", ev => showTip(ev, `<b>${dows[d]}, ${h}:00–${h + 1}:00</b><br>Нарушений за час: ${fmt(v, 2)}`));
      r.addEventListener("mouseleave", hideTip);
    });
  });
  const by = m.t + 7 * (cell + gap);
  for (let h = 0; h < 24; h += 3) el("text", {x: m.l + h * cw + cw / 2, y: by + 12, "text-anchor": "middle"}, svg).textContent = h;
  const lx = m.l, ly = by + 24, lw = 160;
  for (let i = 0; i < RAMP.length; i++) el("rect", {x: lx + i * lw / RAMP.length, y: ly, width: lw / RAMP.length, height: 8, fill: rampColor(i / (RAMP.length - 1))}, svg);
  el("text", {x: lx, y: ly + 20}, svg).textContent = "0";
  el("text", {x: lx + lw, y: ly + 20, "text-anchor": "end"}, svg).textContent = fmt(max, 1);
  el("text", {x: lx + lw + 10, y: ly + 8}, svg).textContent = "нарушений за час";
}

function scatter(svg, pts) {
  const W = 520, H = 260, m = {l: 40, r: 12, t: 10, b: 34};
  clear(svg, W, H);
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const ym = niceMax(Math.max(...pts.map(p => p[1]), 1) * 1.05);
  const x = v => m.l + iw * v, y = v => m.t + ih * (1 - v / ym);
  const nt = tickCount(ym);
  for (let k = 0; k <= nt; k++) {
    const v = ym * k / nt;
    el("line", {x1: m.l, x2: m.l + iw, y1: y(v), y2: y(v), stroke: k ? css("--grid") : css("--axis")}, svg);
    el("text", {x: m.l - 6, y: y(v) + 4, "text-anchor": "end"}, svg).textContent = fmt(v, v % 1 ? 1 : 0);
  }
  for (let k = 0; k <= 4; k++) el("text", {x: x(k / 4), y: H - 16, "text-anchor": "middle"}, svg).textContent = (k * 25) + " %";
  el("text", {x: m.l + iw / 2, y: H - 2, "text-anchor": "middle"}, svg).textContent = "загрузка парковок за час";
  const thr = DATA.params.near_full;
  el("line", {x1: x(thr), x2: x(thr), y1: m.t, y2: m.t + ih, stroke: css("--axis"), "stroke-dasharray": "3 3"}, svg);
  el("text", {x: x(thr) - 4, y: m.t + 10, "text-anchor": "end"}, svg).textContent = "≥ " + Math.round(thr * 100) + " %";
  const g = el("g", {}, svg);
  pts.forEach(p => el("circle", {cx: x(p[0]), cy: y(p[1]), r: 4, fill: css("--dot"), "fill-opacity": 0.55, stroke: css("--surface-1"), "stroke-width": 1}, g));
  const ring = el("circle", {r: 6, fill: "none", stroke: css("--text-primary"), "stroke-width": 1.5, visibility: "hidden"}, svg);
  const hit = el("rect", {x: m.l, y: m.t, width: iw, height: ih, fill: "transparent"}, svg);
  hit.addEventListener("mousemove", ev => {
    const q = svgPoint(svg, ev); let best = null, bd = 400;
    pts.forEach(p => { const d = (x(p[0]) - q.x) ** 2 + (y(p[1]) - q.y) ** 2; if (d < bd) { bd = d; best = p; } });
    if (!best) { ring.setAttribute("visibility", "hidden"); hideTip(); return; }
    ring.setAttribute("cx", x(best[0])); ring.setAttribute("cy", y(best[1])); ring.setAttribute("visibility", "visible");
    showTip(ev, `<b>${best[2]}</b><br>Загрузка: ${pct(best[0])}<br>Нарушений: ${best[1]}`);
  });
  hit.addEventListener("mouseleave", () => { ring.setAttribute("visibility", "hidden"); hideTip(); });
}

function table(tbl, head, rows, attrs) {
  tbl.innerHTML = "<thead><tr>" + head.map(h => Array.isArray(h) ? `<th data-k="${h[1]}">${esc(h[0])}</th>` : `<th>${esc(h)}</th>`).join("") +
    "</tr></thead><tbody>" + rows.map((r, i) => `<tr${attrs ? attrs(i) : ""}>` + r.map(c => `<td>${c}</td>`).join("") + "</tr>").join("") + "</tbody>";
}
function tiles(target, list) {
  $(target).innerHTML = list.map(([k, v, h]) => `<div class="tile"><div class="k">${esc(k)}</div><div class="v">${v}</div><div class="h">${esc(h)}</div></div>`).join("");
}

/* ---------- сборка страницы ---------- */
let sortKey = "capacity", sortDir = -1;
function drawAreasTable() {
  const rows = INFRA.areas.slice().sort((a, b) => {
    const va = a[sortKey], vb = b[sortKey];
    if (typeof va === "string") return sortDir * va.localeCompare(vb, "ru");
    return sortDir * ((va ?? -1) - (vb ?? -1));
  });
  table($("areas"), [["Район", "area"], ["Сегментов", "segments"], ["Мест", "capacity"], ["Для инвалидов", "disabled"],
      ["Кромка, км", "edge_km"], ["Зон", "zones"], ["Тариф (медиана)", "price_median"]],
    rows.map(a => [esc(a.area) + (AN.areas[a.area] ? " ●" : ""), int(a.segments), int(a.capacity), int(a.disabled),
      fmt(a.edge_km, 1), int(a.zones), rub(a.price_median)]),
    i => ` data-area="${esc(rows[i].area)}"`);
  $("areas").querySelectorAll("th[data-k]").forEach(th => th.addEventListener("click", () => {
    const k = th.dataset.k; sortDir = sortKey === k ? -sortDir : (k === "area" ? 1 : -1); sortKey = k; drawAreasTable();
  }));
  $("areas").querySelectorAll("tr[data-area]").forEach(tr => tr.addEventListener("click", () => selectArea(tr.dataset.area)));
}
function selectArea(area) {
  $("area").value = area; draw(); window.scrollTo({top: 0, behavior: "smooth"});
}

function priceMetric(segs) {
  const ps = segs.map(s => s.price).filter(p => p != null).sort((a, b) => a - b);
  const lo = quantile(ps, 0.05), hi = quantile(ps, 0.95);
  if (lo == null || hi == null) return null;
  return {label: "Тариф", lo: rub(lo), hi: rub(hi), value: s => s.price == null ? null : (hi > lo ? (s.price - lo) / (hi - lo) : 0.5)};
}
function capacityMetric(segs) {
  const cs = segs.map(s => s.cap).filter(c => c != null).sort((a, b) => a - b);
  const hi = quantile(cs, 0.95) || 1;
  return {label: "Мест в сегменте", lo: "0", hi: int(hi) + "+", value: s => s.cap == null ? null : s.cap / hi};
}

function draw() {
  const key = $("area").value, city = key === CITY;
  const segsAll = INFRA.segments;
  if (!segsAll.length) {
    tiles("tiles", [["Справочник парковок", "—", "не загружен: выполните parkan fetch-623"]]);
    return;
  }
  const an = city ? null : AN.areas[key];
  let segs = city ? segsAll : segsAll.filter(s => s.area === key);
  if (an) segs = segs.map(s => ({...s, occ: an.seg[s.id]?.occ ?? null, viol: an.seg[s.id]?.viol ?? 0}));
  const ai = city ? INFRA.city : INFRA.areas.find(a => a.area === key) || {};
  tiles("tiles", [
    ["Парковочных мест", int(ai.capacity), city ? `в ${int(ai.areas)} районах` : (ai.adm_area || "")],
    ["Сегментов", int(ai.segments), city ? `${int(ai.zones)} парковочных зон` : `${int(ai.zones)} зон`],
    ["Мест для инвалидов", int(ai.disabled), ai.capacity ? fmt(ai.disabled / ai.capacity * 100, 1) + " % мест" : ""],
    ["Длина кромки", fmt(ai.edge_km, 1) + " км", ai.edge_km ? fmt(ai.capacity / ai.edge_km, 0) + " мест на км" : ""],
    ["Тариф, медиана", rub(ai.price_median), "легковые, будни, пиковый час"],
  ]);
  $("map-title").textContent = city ? "Карта парковок Москвы" : "Карта парковок: " + key;
  const metric = an ? {label: "Средняя загрузка", lo: "0 %", hi: "100 %", value: s => s.occ}
    : (priceMetric(segs) || capacityMetric(segs));
  $("map-note").textContent = an ? "Сегменты окрашены по средней загрузке за период; точки — нарушения с координатами."
    : (metric.label === "Тариф" ? "Сегменты окрашены по почасовому тарифу." : "Сегменты окрашены по числу мест.") +
      (city ? " Выберите район, чтобы приблизить." : "");
  drawMap(segs, {metric, points: an ? an.points : null});

  $("city-block").hidden = !city;
  if (city) {
    const top = INFRA.areas.filter(a => a.area !== "Без района").slice(0, 20);
    hbars($("top-bars"), top.map(a => ({label: a.area, value: a.capacity, area: a.area,
      tip: `<b>${esc(a.area)}</b><br>Мест: ${int(a.capacity)}<br>Сегментов: ${int(a.segments)}<br>Тариф: ${rub(a.price_median)}`})),
      {valueFmt: int, onClick: it => selectArea(it.area)});
  }
  $("areas-card").hidden = !city;

  const anyAnalytics = Object.keys(AN.areas).length > 0;
  $("pending").hidden = !!an;
  $("pending-text").textContent = anyAnalytics
    ? (city ? "Аналитика загрузки и нарушений есть для районов, отмеченных ● в таблице." : "Для этого района данных о загрузке и нарушениях пока нет.")
    : "Открытых машинных источников этих данных сейчас нет: справочник показывает вместимость, но не занятость.";
  $("analytics").hidden = !an;
  if (an) drawAnalytics(an);
}

function drawAnalytics(a) {
  const s = a.summary;
  tiles("a-tiles", [
    ["Нарушений в сутки", fmt(s.violations_per_day, 1), `всего ${s.violations} за ${s.days} дн.`],
    ["На 100 мест в сутки", fmt(s.violations_per_100_spaces_per_day, 1), `${int(s.capacity_total)} мест в районе`],
    ["На 1 км кромки в сутки", fmt(s.violations_per_km_per_day, 1), `${fmt(s.edge_km, 2)} км кромки`],
    ["Средняя загрузка", pct(s.mean_occupancy_rate), "по часам с данными"],
    ["При загрузке ≥ " + Math.round(DATA.params.near_full * 100) + " %", pct(s.near_full_share), "доля нарушений"],
    ["Связь загрузки и нарушений", s.corr_occupancy_violations == null ? "—" : "r = " + fmt(s.corr_occupancy_violations, 2), "корреляция Пирсона по часам"],
    ["От начала пика до нарушения", s.median_minutes_from_peak_start == null ? "—" : fmt(s.median_minutes_from_peak_start, 0) + " мин", "медиана"],
  ]);
  const c1 = css("--series-1"), c2 = css("--series-2");
  lineChart($("prof-occ"), [{name: "Будни", color: c1, values: a.profile.weekday.occ}, {name: "Выходные", color: c2, values: a.profile.weekend.occ}],
    {yMax: 1, yFmt: v => Math.round(v * 100), tipFmt: pct});
  lineChart($("prof-viol"), [{name: "Будни", color: c1, values: a.profile.weekday.viol}, {name: "Выходные", color: c2, values: a.profile.weekend.viol}],
    {yFmt: v => fmt(v, v % 1 ? 1 : 0), tipFmt: v => fmt(v, 2)});
  heatmap($("heat"), a.heat, DATA.dow);
  scatter($("scatter"), a.scatter);
  table($("types"), ["Тип нарушения", "Сообщение", "Проверено", "Постановление", "Отклонено", "Всего"],
    a.types.map(t => [esc(t[0]), ...t.slice(1).map(int)]));
}

function init() {
  const parts = [];
  if (INFRA.loaded_at) parts.push(`Справочник обновлён ${INFRA.loaded_at}` + (INFRA.version ? `, версия набора ${INFRA.version}` : ""));
  if (AN.period && AN.period[0]) parts.push(`загрузка и нарушения: ${AN.period[0]} — ${AN.period[1]}`);
  $("sub").textContent = parts.join(" · ") || "Нет данных";
  const names = INFRA.areas.map(a => a.area).sort((a, b) => a.localeCompare(b, "ru"));
  $("area").innerHTML = `<option value="${CITY}">Вся Москва</option>` + names.map(n => `<option value="${esc(n)}">${esc(n)}${AN.areas[n] ? " ●" : ""}</option>`).join("");
  $("area").addEventListener("change", draw);
  drawAreasTable();
  const c = AN.coverage, share = v => c && c.total ? Math.round(v / c.total * 100) + " %" : "—";
  const lc = INFRA.last_changes || {};
  $("coverage").innerHTML = (Object.keys(lc).length ? `Последнее обновление справочника: новых сегментов ${lc.added || 0}, изменённых ${lc.modified || 0}, удалённых ${lc.removed || 0}. ` : "") +
    (c && c.total ? `Событий нарушений: ${c.total}. Сопоставлено с сегментом: ${c.matched} (${share(c.matched)}),
      со снимком загрузки в окне ±${DATA.params.window_min} мин: ${c.with_occupancy} (${share(c.with_occupancy)}).
      ${DATA.params.include_rejected ? "Отклонённые сообщения учтены." : "Отклонённые сообщения в число нарушений не входят."}` : "");
  table($("sources"), ["Источник", "Записей"], DATA.sources.map(s => [esc(s[0]), int(s[1])]));
  draw();
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(draw, 150); });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
  new MutationObserver(draw).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
}
init();
</script>
</body>
</html>
"""
