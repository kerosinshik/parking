"""Самодостаточный HTML-отчёт (без внешних зависимостей): карта, heatmap, профиль дня, связь."""

from __future__ import annotations

import html
import json
from pathlib import Path

from .mart import MartParams, area_summary

DOW_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MAX_MAP_POINTS = 4000


def _num(v, nd=3):
    return None if v is None else round(float(v), nd)


def collect_data(con, params: MartParams | None = None) -> dict:
    params = params or MartParams()
    counted = "status <> 'отклонено'" if not params.include_rejected else "TRUE"
    summary = area_summary(con, params)
    areas = []
    for s in summary:
        a = s["area"]
        # профиль дня: среднее по часам отдельно для будней и выходных (dow: 0 = вс, 6 = сб)
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
        segs = [{"id": r[0], "name": r[1], "cap": r[2], "geom": json.loads(r[3]) if r[3] else None,
                 "lat": r[4], "lon": r[5], "occ": _num(r[6]), "viol": r[7]}
                for r in con.execute(f"""
            SELECT s.parking_id, coalesce(s.name, s.address, s.parking_id), s.capacity_total, s.geometry,
                   s.latitude, s.longitude,
                   (SELECT sum(occupied_spaces) / nullif(sum(total_spaces), 0)
                      FROM occupancy_snapshots o WHERE o.parking_id = s.parking_id),
                   (SELECT count(*) FROM violation_occupancy v
                     WHERE v.parking_id = s.parking_id AND {counted})
            FROM current_segments s WHERE s.municipality_or_area = ?
        """, [a]).fetchall()]
        points = [[round(r[0], 6), round(r[1], 6)] for r in con.execute(f"""
            SELECT e.latitude, e.longitude FROM violation_occupancy v
            JOIN violation_events e USING (source_system, violation_id)
            WHERE v.area = ? AND e.latitude IS NOT NULL AND {counted.replace('status', 'v.status')}
            LIMIT {MAX_MAP_POINTS}
        """, [a]).fetchall()]
        types = [list(r) for r in con.execute("""
            SELECT violation_type,
                   sum(n) FILTER (WHERE status = 'сообщение'),
                   sum(n) FILTER (WHERE status = 'проверено'),
                   sum(n) FILTER (WHERE status = 'постановление'),
                   sum(n) FILTER (WHERE status = 'отклонено'),
                   sum(n)
            FROM hourly_area_type_metrics WHERE area = ? GROUP BY 1 ORDER BY 6 DESC
        """, [a]).fetchall()]
        types = [[t[0]] + [int(x or 0) for x in t[1:]] for t in types]
        areas.append({"summary": {k: (_num(v) if isinstance(v, float) else v) for k, v in s.items()},
                      "profile": profile, "heat": heat, "scatter": scatter,
                      "segments": segs, "points": points, "types": types})
    period = con.execute(
        "SELECT min(hour_start), max(hour_start) FROM hourly_area_metrics").fetchone()
    cov = con.execute("""
        SELECT count(*), count(parking_id), count(occupancy_rate), count(*) FILTER (WHERE area IS NULL)
        FROM violation_occupancy
    """).fetchone()
    sources = con.execute("""
        SELECT 'occupancy: ' || source_type || ' / ' || source_name, count(*) FROM occupancy_snapshots GROUP BY 1
        UNION ALL
        SELECT 'нарушения: ' || source_system, count(*) FROM violation_events GROUP BY 1
        UNION ALL
        SELECT 'справочник парковок, версия ' || coalesce(source_version, '?'), count(*)
        FROM current_segments GROUP BY 1
        ORDER BY 1
    """).fetchall()
    return {
        "period": [period[0].strftime("%d.%m.%Y") if period[0] else None,
                   period[1].strftime("%d.%m.%Y") if period[1] else None],
        "coverage": {"total": cov[0], "matched": cov[1], "with_occupancy": cov[2], "no_area": cov[3]},
        "sources": [[s[0], s[1]] for s in sources],
        "params": {"window_min": params.window_min, "max_distance_m": params.max_distance_m,
                   "near_full": params.near_full, "include_rejected": params.include_rejected},
        "dow": DOW_RU,
        "areas": areas,
    }


def render(con, out_path, params: MartParams | None = None, title: str = "Парковки и нарушения") -> Path:
    data = collect_data(con, params)
    payload = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
    page = TEMPLATE.replace("__TITLE__", html.escape(title)).replace("__DATA__", payload)
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
  background: var(--surface-1); color: var(--text-primary); }
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
.legend { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px; color: var(--text-secondary); margin: 4px 0 8px; }
.sw { display: inline-block; width: 12px; height: 3px; border-radius: 2px; vertical-align: middle; margin-right: 6px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: right; padding: 6px 8px; border-bottom: 1px solid var(--grid); font-variant-numeric: tabular-nums; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--text-secondary); font-weight: 500; }
.scroll { overflow-x: auto; }
#tip { position: fixed; pointer-events: none; background: var(--surface-1); color: var(--text-primary);
  border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; font-size: 12px;
  box-shadow: 0 4px 16px rgba(0,0,0,.15); display: none; z-index: 10; white-space: nowrap; }
#tip b { font-weight: 600; }
.warn { border-left: 3px solid var(--axis); padding-left: 10px; }
</style>
</head>
<body>
<main>
  <h1>__TITLE__</h1>
  <p class="sub" id="period"></p>
  <div class="bar">
    <label for="area">Район</label>
    <select id="area"></select>
  </div>
  <div class="tiles" id="tiles"></div>

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
    <h2>Карта</h2>
    <p class="note">Парковочные сегменты окрашены по средней загрузке за период; точки — нарушения с координатами.</p>
    <svg id="map"></svg>
    <div id="map-legend"></div>
  </div>

  <div class="card">
    <h2>Типы нарушений и статусы</h2>
    <div class="scroll"><table id="types"></table></div>
  </div>

  <div class="card">
    <h2>Все районы</h2>
    <div class="scroll"><table id="areas"></table></div>
  </div>

  <div class="card warn">
    <h2>Покрытие данных и ограничения</h2>
    <div id="coverage" class="note"></div>
    <p class="note">Высокая загрузка и число нарушений могут быть связаны, но это не доказывает, что одно вызывает другое.
      На результат влияют характер района, тариф, погода, мероприятия, работа эвакуаторов и контролёров,
      неполное покрытие камерами. Сообщение гражданина, проверенный случай и постановление — разные события.</p>
    <div class="scroll"><table id="sources"></table></div>
  </div>
</main>
<div id="tip"></div>
<script>
const DATA = __DATA__;
const RAMP = ["#cde2fb","#b7d3f6","#9ec5f4","#86b6ef","#6da7ec","#5598e7","#3987e5","#2a78d6","#256abf","#1c5cab","#184f95","#104281","#0d366b"];
const NS = "http://www.w3.org/2000/svg";
const $ = id => document.getElementById(id);
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const fmt = (v, d = 1) => v == null ? "—" : Number(v).toLocaleString("ru-RU", {maximumFractionDigits: d, minimumFractionDigits: d});
const pct = v => v == null ? "—" : fmt(v * 100, 0) + " %";
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function rampColor(t) {
  if (t == null || isNaN(t)) return css("--grid");
  t = Math.max(0, Math.min(1, t));
  if (css("--ramp-dir") === "-1") t = 1 - t;
  return RAMP[Math.round(t * (RAMP.length - 1))];
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
  tip.style.left = x + "px"; tip.style.top = y + "px";
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

function coordsOf(geom) {
  if (!geom) return [];
  const t = geom.type, c = geom.coordinates;
  if (t === "Point") return [[c]];
  if (t === "LineString" || t === "MultiPoint") return [c];
  if (t === "MultiLineString" || t === "Polygon") return c;
  if (t === "MultiPolygon") return c.flat();
  return [];
}
function mapChart(svg, segs, points) {
  const W = 1000, H = 520, pad = 24;
  clear(svg, W, H);
  const lines = segs.map(s => coordsOf(s.geom).length ? coordsOf(s.geom) : (s.lat != null ? [[[s.lon, s.lat]]] : []));
  const all = lines.flat(2).concat(points.map(p => [p[1], p[0]]));
  if (!all.length) { el("text", {x: W / 2, y: H / 2, "text-anchor": "middle"}, svg).textContent = "нет координат"; return; }
  let [x0, x1, y0, y1] = [Infinity, -Infinity, Infinity, -Infinity];
  all.forEach(([lon, lat]) => { x0 = Math.min(x0, lon); x1 = Math.max(x1, lon); y0 = Math.min(y0, lat); y1 = Math.max(y1, lat); });
  const k = Math.cos((y0 + y1) / 2 * Math.PI / 180);
  const sx = (x1 - x0) * k || 1e-4, sy = (y1 - y0) || 1e-4;
  const s = Math.min((W - 2 * pad) / sx, (H - 2 * pad) / sy);
  const ox = (W - sx * s) / 2, oy = (H - sy * s) / 2;
  const px = lon => ox + (lon - x0) * k * s, py = lat => H - oy - (lat - y0) * s;
  const gp = el("g", {}, svg);
  points.forEach(p => el("circle", {cx: px(p[1]), cy: py(p[0]), r: 2.5, fill: css("--text-secondary"), "fill-opacity": 0.45}, gp));
  segs.forEach((sg, i) => {
    lines[i].forEach(line => {
      const d = line.map((c, j) => (j ? "L" : "M") + px(c[0]) + "," + py(c[1])).join("");
      const single = line.length === 1;
      const vis = single
        ? el("circle", {cx: px(line[0][0]), cy: py(line[0][1]), r: 6, fill: rampColor(sg.occ), stroke: css("--surface-1"), "stroke-width": 2}, svg)
        : el("path", {d, fill: "none", stroke: rampColor(sg.occ), "stroke-width": 6, "stroke-linecap": "round"}, svg);
      const hit = single
        ? el("circle", {cx: px(line[0][0]), cy: py(line[0][1]), r: 12, fill: "transparent"}, svg)
        : el("path", {d, fill: "none", stroke: "transparent", "stroke-width": 16}, svg);
      hit.addEventListener("mousemove", ev => showTip(ev,
        `<b>${esc(sg.name)}</b><br>Мест: ${sg.cap ?? "—"}<br>Средняя загрузка: ${pct(sg.occ)}<br>Нарушений: ${sg.viol}`));
      hit.addEventListener("mouseleave", hideTip);
    });
  });
  const lg = $("map-legend");
  lg.innerHTML = `<div class="legend"><span>Средняя загрузка: 0 %</span>` +
    RAMP.map((_, i) => `<i style="display:inline-block;width:14px;height:8px;background:${rampColor(i / (RAMP.length - 1))}"></i>`).join("") +
    `<span>100 %</span><span><i class="sw" style="width:6px;height:6px;border-radius:50%;background:var(--text-secondary)"></i>нарушение</span></div>`;
}

function table(tbl, head, rows) {
  tbl.innerHTML = "<thead><tr>" + head.map(h => `<th>${esc(h)}</th>`).join("") + "</tr></thead><tbody>" +
    rows.map(r => "<tr>" + r.map(c => `<td>${c}</td>`).join("") + "</tr>").join("") + "</tbody>";
}

function tiles(s) {
  const t = [
    ["Нарушений в сутки", fmt(s.violations_per_day, 1), `всего ${s.violations} за ${s.days} дн.`],
    ["На 100 мест в сутки", fmt(s.violations_per_100_spaces_per_day, 1), `${s.capacity_total ?? "—"} мест в районе`],
    ["На 1 км кромки в сутки", fmt(s.violations_per_km_per_day, 1), `${fmt(s.edge_km, 2)} км кромки`],
    ["Средняя загрузка", pct(s.mean_occupancy_rate), "по часам с данными"],
    ["При загрузке ≥ " + Math.round(DATA.params.near_full * 100) + " %", pct(s.near_full_share), "доля нарушений"],
    ["Связь загрузки и нарушений", s.corr_occupancy_violations == null ? "—" : "r = " + fmt(s.corr_occupancy_violations, 2), "корреляция Пирсона по часам"],
    ["От начала пика до нарушения", s.median_minutes_from_peak_start == null ? "—" : fmt(s.median_minutes_from_peak_start, 0) + " мин", "медиана"],
  ];
  $("tiles").innerHTML = t.map(([k, v, h]) => `<div class="tile"><div class="k">${esc(k)}</div><div class="v">${v}</div><div class="h">${esc(h)}</div></div>`).join("");
}

function draw() {
  const a = DATA.areas[$("area").selectedIndex];
  if (!a) { $("tiles").innerHTML = "<p class='note'>Нет данных. Загрузите справочник, occupancy и нарушения, затем выполните build-mart.</p>"; return; }
  tiles(a.summary);
  const c1 = css("--series-1"), c2 = css("--series-2");
  lineChart($("prof-occ"), [{name: "Будни", color: c1, values: a.profile.weekday.occ}, {name: "Выходные", color: c2, values: a.profile.weekend.occ}],
    {yMax: 1, yFmt: v => Math.round(v * 100), tipFmt: pct});
  lineChart($("prof-viol"), [{name: "Будни", color: c1, values: a.profile.weekday.viol}, {name: "Выходные", color: c2, values: a.profile.weekend.viol}],
    {yFmt: v => fmt(v, 1), tipFmt: v => fmt(v, 2)});
  heatmap($("heat"), a.heat, DATA.dow);
  scatter($("scatter"), a.scatter);
  mapChart($("map"), a.segments, a.points);
  table($("types"), ["Тип нарушения", "Сообщение", "Проверено", "Постановление", "Отклонено", "Всего"],
    a.types.map(t => [esc(t[0]), ...t.slice(1).map(v => v.toLocaleString("ru-RU"))]));
}

function init() {
  $("period").textContent = DATA.period[0] ? `Период: ${DATA.period[0]} — ${DATA.period[1]}` : "Нет данных";
  $("area").innerHTML = DATA.areas.map((a, i) => `<option value="${i}">${esc(a.summary.area)}</option>`).join("");
  $("area").addEventListener("change", draw);
  table($("areas"), ["Район", "Нарушений в сутки", "На 100 мест в сутки", "На 1 км в сутки", "Средняя загрузка", "Доля при высокой загрузке", "r"],
    DATA.areas.map(a => { const s = a.summary; return [esc(s.area), fmt(s.violations_per_day), fmt(s.violations_per_100_spaces_per_day),
      fmt(s.violations_per_km_per_day), pct(s.mean_occupancy_rate), pct(s.near_full_share), fmt(s.corr_occupancy_violations, 2)]; }));
  const c = DATA.coverage, share = v => c.total ? Math.round(v / c.total * 100) + " %" : "—";
  $("coverage").innerHTML = `Событий нарушений: ${c.total}. Сопоставлено с парковочным сегментом: ${c.matched} (${share(c.matched)}),
    со снимком загрузки в окне ±${DATA.params.window_min} мин: ${c.with_occupancy} (${share(c.with_occupancy)}).
    Без района (не попали в радиус ${DATA.params.max_distance_m} м и район не указан): ${c.no_area}.
    ${DATA.params.include_rejected ? "Отклонённые сообщения учтены." : "Отклонённые сообщения в число нарушений не входят."}`;
  table($("sources"), ["Источник", "Записей"], DATA.sources.map(s => [esc(s[0]), s[1].toLocaleString("ru-RU")]));
  draw();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
  new MutationObserver(draw).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
}
init();
</script>
</body>
</html>
"""
