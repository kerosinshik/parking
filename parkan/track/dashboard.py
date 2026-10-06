"""Дашборд «Пульс города»: три живых потока data.mos.ru по журналу изменений."""

from __future__ import annotations

import html
import json
from datetime import date
from pathlib import Path

from ..report import to_fragment
from ..timeutil import now_msk
from . import metrics
from .streams import STREAMS


def collect(con, state_dir, parking_con=None, today: date | None = None) -> dict:
    today = today or now_msk().date()
    ids = [ds.id for s in STREAMS.values() for ds in s.datasets]
    return {
        "today": today.isoformat(),
        "generated": now_msk().strftime("%d.%m.%Y %H:%M"),
        "licenses": metrics.licenses(con, state_dir, today),
        "lots": metrics.parking_lots(con, state_dir, today, parking_con),
        "works": metrics.works(con, state_dir, today, parking_con),
        "log": metrics.sync_log(con, state_dir, ids),
        "datasets": [{"id": ds.id, "title": ds.title, "stream": s.title} for s in STREAMS.values() for ds in s.datasets],
    }


def render_dashboard(con, state_dir, out_path, *, parking_con=None, title: str = "Пульс города",
                     fragment: bool = False, today: date | None = None) -> Path:
    data = collect(con, state_dir, parking_con, today)
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
/* Рабочая панель: сводка сверху, вкладки по потокам, графики и таблицы на единой сетке */
:root {
  color-scheme: light;
  --page: #f6f7f5; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10); --hover: #eef0ec;
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --up: #006300; --down: #b4282c;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10); --hover: #242423;
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --up: #0ca30c; --down: #e66767;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10); --hover: #242423;
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --up: #0ca30c; --down: #e66767;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1160px; margin: 0 auto; padding-block: 24px 48px; padding-inline: 16px; }
h1 { font-size: 22px; margin: 0 0 4px; text-wrap: balance; }
h2 { font-size: 15px; margin: 0 0 4px; }
.sub, .note { color: var(--ink-2); }
.sub { margin: 0 0 16px; }
.note { font-size: 13px; margin: 2px 0 10px; }
nav { display: flex; gap: 4px; flex-wrap: wrap; border-bottom: 1px solid var(--grid); margin-bottom: 16px; }
nav button { font: inherit; background: none; border: 0; border-bottom: 2px solid transparent; color: var(--ink-2);
  padding: 8px 12px; cursor: pointer; }
nav button[aria-selected="true"] { color: var(--ink); border-bottom-color: var(--s1); font-weight: 600; }
nav button:focus-visible { outline: 2px solid var(--s1); outline-offset: 2px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 16px; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; min-width: 0; }
.tile .k { color: var(--ink-2); font-size: 12px; }
.tile .v { font-size: 24px; font-weight: 600; margin-top: 2px; }
.tile .h { color: var(--muted); font-size: 12px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin-bottom: 16px; min-width: 0; }
.grid2 { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }
@media (max-width: 860px) { .grid2 { grid-template-columns: minmax(0, 1fr); } }
svg { display: block; width: 100%; height: auto; overflow: visible; }
svg text { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px; color: var(--ink-2); margin: 0 0 6px; }
.sw { display: inline-block; width: 12px; height: 3px; border-radius: 2px; vertical-align: middle; margin-right: 6px; }
.scroll { overflow-x: auto; }
.tall { max-height: 460px; overflow: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: right; padding: 6px 8px; border-bottom: 1px solid var(--grid); font-variant-numeric: tabular-nums; vertical-align: top; }
th:first-child, td:first-child, td.l, th.l { text-align: left; }
th { color: var(--ink-2); font-weight: 500; position: sticky; top: 0; background: var(--surface); }
tbody tr:hover td { background: var(--hover); }
.pos { color: var(--up); } .neg { color: var(--down); }
.pill { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--axis); color: var(--ink-2); white-space: nowrap; }
.pill.open { border-color: var(--up); color: var(--up); } .pill.close { border-color: var(--down); color: var(--down); }
.empty { color: var(--muted); padding: 24px 0; }
#tip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink); border: 1px solid var(--border);
  border-radius: 8px; padding: 6px 10px; font-size: 12px; box-shadow: 0 4px 16px rgba(0,0,0,.15); display: none; z-index: 10; max-width: 320px; }
a { color: var(--s1); }
@media (prefers-reduced-motion: reduce) { * { scroll-behavior: auto !important; } }
</style>
</head>
<body>
<main>
  <h1>__TITLE__</h1>
  <p class="sub" id="sub"></p>
  <nav role="tablist" id="tabs"></nav>
  <section id="tab-licenses" role="tabpanel"></section>
  <section id="tab-lots" role="tabpanel" hidden></section>
  <section id="tab-works" role="tabpanel" hidden></section>
  <section id="tab-log" role="tabpanel" hidden></section>
</main>
<div id="tip"></div>
<script>
const DATA = __DATA__;
const NS = "http://www.w3.org/2000/svg";
const $ = id => document.getElementById(id);
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const int = v => v == null ? "—" : Math.round(v).toLocaleString("ru-RU");
const pct = v => v == null ? "—" : Math.round(v * 100) + " %";
const rub = v => v == null ? "—" : Math.round(v).toLocaleString("ru-RU") + " ₽";
const mln = v => v == null ? "—" : (v / 1e6).toLocaleString("ru-RU", {maximumFractionDigits: 2}) + " млн ₽";
const sgn = v => (v > 0 ? "+" : "") + int(v);
const dmy = iso => { const [y, m, d] = iso.split("-"); return `${d}.${m}`; };
const tip = $("tip");
function showTip(ev, h) {
  tip.innerHTML = h; tip.style.display = "block";
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 14;
  tip.style.left = Math.max(8, x) + "px"; tip.style.top = Math.max(8, y) + "px";
}
const hideTip = () => tip.style.display = "none";
function el(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
function niceMax(v) {
  if (!v || v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v))), n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
}
function ticks(max) { const lead = Math.round(max / Math.pow(10, Math.floor(Math.log10(max)))); return lead === 2 ? 4 : 5; }

/* Линейный график по датам: общая ось Y, перекрестие и подсказка со всеми сериями */
function lineChart(svg, labels, series, {yFmt = int, tipFmt = int, every = 7, labelFmt = dmy} = {}) {
  const W = 560, H = 230, m = {l: 48, r: 86, t: 10, b: 26};
  svg.innerHTML = ""; svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const iw = W - m.l - m.r, ih = H - m.t - m.b, n = labels.length;
  const vals = series.flatMap(s => s.values).filter(v => v != null);
  const ym = niceMax(Math.max(...vals, 0) * 1.08), nt = ticks(ym);
  const x = i => m.l + (n > 1 ? iw * i / (n - 1) : iw / 2), y = v => m.t + ih * (1 - v / ym);
  for (let k = 0; k <= nt; k++) {
    const v = ym * k / nt;
    el("line", {x1: m.l, x2: m.l + iw, y1: y(v), y2: y(v), stroke: k ? css("--grid") : css("--axis")}, svg);
    el("text", {x: m.l - 6, y: y(v) + 4, "text-anchor": "end"}, svg).textContent = yFmt(v);
  }
  for (let i = 0; i < n; i += every) el("text", {x: x(i), y: H - 6, "text-anchor": "middle"}, svg).textContent = labelFmt(labels[i]);
  const ends = [];
  series.forEach(s => {
    let d = "", pen = false;
    s.values.forEach((v, i) => { if (v == null) { pen = false; return; } d += (pen ? "L" : "M") + x(i) + "," + y(v); pen = true; });
    el("path", {d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round"}, svg);
    const last = s.values.map((v, i) => [v, i]).filter(p => p[0] != null).pop();
    if (last) ends.push({x: x(last[1]), y: y(last[0]), name: s.name});
  });
  ends.sort((a, b) => a.y - b.y);
  for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 13) ends[i].y = ends[i - 1].y + 13;
  ends.forEach(e => { const t = el("text", {x: e.x + 6, y: e.y + 4}, svg); t.textContent = e.name; t.style.fill = css("--ink-2"); });
  const cross = el("line", {y1: m.t, y2: m.t + ih, stroke: css("--axis"), visibility: "hidden"}, svg);
  const dots = series.map(s => el("circle", {r: 4, fill: s.color, stroke: css("--surface"), "stroke-width": 2, visibility: "hidden"}, svg));
  const hit = el("rect", {x: m.l, y: m.t, width: iw, height: ih, fill: "transparent"}, svg);
  hit.addEventListener("mousemove", ev => {
    const pt = svg.createSVGPoint(); pt.x = ev.clientX; pt.y = ev.clientY;
    const p = pt.matrixTransform(svg.getScreenCTM().inverse());
    const i = Math.max(0, Math.min(n - 1, Math.round((p.x - m.l) / iw * (n - 1))));
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    series.forEach((s, k) => {
      const v = s.values[i];
      if (v == null) { dots[k].setAttribute("visibility", "hidden"); return; }
      dots[k].setAttribute("cx", x(i)); dots[k].setAttribute("cy", y(v)); dots[k].setAttribute("visibility", "visible");
    });
    showTip(ev, `<b>${labelFmt(labels[i])}${labels[i].length === 10 ? "." + labels[i].slice(0, 4) : ""}</b><br>` +
      series.map(s => `<i class="sw" style="background:${s.color}"></i>${esc(s.name)}: ${tipFmt(s.values[i])}`).join("<br>"));
  });
  hit.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); dots.forEach(d => d.setAttribute("visibility", "hidden")); hideTip(); });
}
function legend(series) { return `<div class="legend">${series.map(s => `<span><i class="sw" style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>`; }
function tiles(list) { return `<div class="tiles">${list.map(([k, v, h]) => `<div class="tile"><div class="k">${esc(k)}</div><div class="v">${v}</div><div class="h">${esc(h || "")}</div></div>`).join("")}</div>`; }
function table(head, rows, {left = [0]} = {}) {
  return `<table><thead><tr>${head.map((h, i) => `<th class="${left.includes(i) ? "l" : ""}">${esc(h)}</th>`).join("")}</tr></thead><tbody>` +
    rows.map(r => `<tr>${r.map((c, i) => `<td class="${left.includes(i) ? "l" : ""}">${c}</td>`).join("")}</tr>`).join("") + "</tbody></table>";
}
const card = (title, note, body) => `<div class="card"><h2>${esc(title)}</h2>${note ? `<p class="note">${note}</p>` : ""}${body}</div>`;
const empty = what => `<div class="card empty">${esc(what)}: журнал ещё не собран. Запустите <code>parkan track-sync</code>.</div>`;
// «Общество с ограниченной ответственностью "Лента"» -> «ООО "Лента"»
const ORG = [[/публичное акционерное общество/gi, "ПАО"], [/непубличное акционерное общество/gi, "АО"],
  [/акционерное общество/gi, "АО"], [/общество с ограниченной ответственностью/gi, "ООО"],
  [/индивидуальный предприниматель/gi, "ИП"], [/федеральное государственное\s+бюджетное\s+учреждение/gi, "ФГБУ"],
  [/государственное бюджетное учреждение/gi, "ГБУ"]];
const org = s => ORG.reduce((a, [re, r]) => a.replace(re, r), String(s ?? "")).replace(/\s+/g, " ").trim();
// «Российская Федерация, город Москва, внутригородская территория муниципальный округ X, улица…» -> «улица…»
const addr = s => String(s ?? "").replace(/^Российская Федерация,\s*/i, "").replace(/^город Москва,\s*/i, "")
  .replace(/^(город Москва,\s*)?внутригородская территория[^,]*,\s*/i, "").replace(/^г\.?\s*Москва,\s*/i, "");
const net = v => `<span class="${v > 0 ? "pos" : v < 0 ? "neg" : ""}">${sgn(v)}</span>`;

/* ---------- общепит и алкоритейл ---------- */
function renderLicenses() {
  const L = DATA.licenses, root = $("tab-licenses");
  if (!L) { root.innerHTML = empty("Лицензии"); return; }
  const o = L.tiles["РПО"], r = L.tiles["РПА"];
  root.innerHTML = tiles([
    ["Общепит: действующих точек", int(o.active), "кафе, бары, рестораны с лицензией"],
    ["Общепит за 30 дней", `${net(o.opened30 - o.closed30)}`, `открылось ${int(o.opened30)}, закрылось ${int(o.closed30)}`],
    ["Алкоритейл: действующих точек", int(r.active), "магазины с лицензией"],
    ["Алкоритейл за 30 дней", `${net(r.opened30 - r.closed30)}`, `открылось ${int(r.opened30)}, закрылось ${int(r.closed30)}`],
  ]) +
  `<div class="grid2">` +
    card("Общепит: открытия и закрытия по неделям", "Точка — пара «юрлицо + адрес». Открытие — первая лицензия точки, закрытие — прекращение последней.",
      legend(sOC()) + `<svg id="lic-rpo"></svg>`) +
    card("Алкоритейл: открытия и закрытия по неделям", "Магазины с лицензией на розничную продажу алкоголя.", legend(sOC()) + `<svg id="lic-rpa"></svg>`) +
  `</div><div class="grid2">` +
    card("Районы за 90 дней", "Сальдо открытий и закрытий точек; по убыванию прироста.",
      `<div class="scroll tall">${table(["Район", "Открылось", "Закрылось", "Сальдо", "Действует"],
        L.districts.map(d => [esc(d.district), int(d.opened), int(d.closed), net(d.net), int(d.active)]))}</div>`) +
    card("Сети и операторы за 90 дней", "Юрлица с наибольшим числом открытий и закрытий.",
      `<div class="scroll tall">${table(["Юрлицо", "Открыто", "Закрыто", "Действует"],
        L.chains.map(c => [esc(org(c.subject)), int(c.opened), int(c.closed), int(c.active)]))}</div>`) +
  `</div>` +
    card("Последние 14 дней", "Открытия и закрытия точек по датам в реестре.",
      `<div class="scroll tall">${table(["Дата", "Событие", "Точка", "Юрлицо", "Район", "Тип"],
        L.recent.map(x => [x.date.split("-").reverse().join("."), `<span class="pill ${x.kind === "открытие" ? "open" : "close"}">${x.kind}</span>`,
          esc(x.name), esc(org(x.subject)), esc(x.district), x.job === "РПО" ? "общепит" : "розница"]), {left: [0, 1, 2, 3, 4, 5]})}</div>`);
  function sOC() { return [{name: "Открытия", color: css("--s1")}, {name: "Закрытия", color: css("--s2")}]; }
  for (const [job, id] of [["РПО", "lic-rpo"], ["РПА", "lic-rpa"]])
    lineChart($(id), L.weeks, [{name: "Открытия", color: css("--s1"), values: L.series[job].opened},
                               {name: "Закрытия", color: css("--s2"), values: L.series[job].closed}], {every: 8});
}

/* ---------- машино-места ---------- */
function renderLots() {
  const P = DATA.lots, root = $("tab-lots");
  if (!P) { root.innerHTML = empty("Торги"); return; }
  const t = P.tiles;
  root.innerHTML = tiles([
    ["На торгах сейчас", int(t.on_sale), "приём заявок открыт"],
    ["Новых лотов за 30 дней", int(t.new30), ""],
    ["Медианная стартовая цена", mln(t.median_price), "лоты на торгах"],
    ["Медиана за м²", rub(t.median_m2), "стартовая цена / площадь"],
    ["Смен этапа в журнале", int(P.stage_changes), "видны со второй синхронизации"],
  ]) +
  `<div class="grid2">` +
    card("Новых лотов в неделю", "Машино-места, выставленные городом на открытые аукционы.", `<svg id="lots-new"></svg>`) +
    card("Медианная стартовая цена по неделям", "Только недели, в которые выставлялись лоты.", `<svg id="lots-price"></svg>`) +
  `</div>` +
    card("Районы", P.tariff_linked ? "Уличный тариф — медианный почасовой тариф ближайшего платного участка (в пределах 300 м), набор № 623." :
      "Связка с уличными тарифами появится, если рядом есть база справочника парковок № 623.",
      `<div class="scroll tall">${table(["Район", "На торгах", "Медиана цены", "Медиана за м²", "Уличный тариф рядом"],
        P.districts.map(d => [esc(d.district), int(d.on_sale), mln(d.median_price), rub(d.median_m2),
          d.street_tariff == null ? "—" : rub(d.street_tariff) + "/ч"]))}</div>`) +
    card("Ближайшие окончания приёма заявок", "Лоты по одному адресу и с одной датой торгов сгруппированы.",
      `<div class="scroll tall">${table(["Адрес", "Район", "Лотов", "Стартовая цена", "Площадь", "Заявки до", "Торги"],
      P.upcoming.map(u => [esc(addr(u.address)), esc(u.district), int(u.lots),
        u.price_min === u.price_max ? mln(u.price_min) : `${mln(u.price_min)} – ${mln(u.price_max)}`,
        u.space == null ? "—" : u.space.toLocaleString("ru-RU") + " м²", esc(u.end || "—"), esc(u.trades || "—")]), {left: [0, 1]})}</div>`);
  lineChart($("lots-new"), P.weeks, [{name: "Лотов", color: css("--s1"), values: P.new}], {every: 8});
  lineChart($("lots-price"), P.weeks, [{name: "Медиана", color: css("--s1"), values: P.median_price}],
    {every: 8, yFmt: v => (v / 1e6).toLocaleString("ru-RU", {maximumFractionDigits: 1}) + " млн", tipFmt: mln});
}

/* ---------- помехи по адресу ---------- */
function renderWorks() {
  const W = DATA.works, root = $("tab-works");
  if (!W) { root.innerHTML = empty("Работы"); return; }
  const e = W.em_tiles || {}, g = W.ew_tiles || {}, pk = W.parking;
  const colors = [css("--s1"), css("--s2"), css("--s3")];
  const emS = Object.entries(W.em_series || {}).map(([k, v], i) => ({name: k, color: colors[i], values: v}));
  const ewS = Object.entries(W.ew_series || {}).map(([k, v], i) => ({name: k, color: colors[i], values: v}));
  root.innerHTML = tiles([
    ["Аварийных работ сейчас", int(e.active), `с отключением абонентов: ${int(e.active_outage)}`],
    ["Новых аварий за 7 дней", int(e.new7), `за 30 дней: ${int(e.new30)}`],
    ["Земляных работ сейчас", int(g.active_earth), `всего уведомлений действует: ${int(g.active_all)}`],
    ["Платных мест под работами", pk ? int(pk.spaces) : "—", pk ? `${pk.segments} участков, ${(pk.spaces / pk.total_spaces * 100).toFixed(1)} % мест · строгая оценка` : "нужен справочник № 623"],
  ]) +
  `<div class="grid2">` +
    card("Новые аварийные работы по дням", "Дата регистрации аварийного вызова, последние 90 дней.", legend(emS) + `<svg id="em-days"></svg>`) +
    card("Новые уведомления о работах по дням", "Земляные работы и временные ограждения.", legend(ewS) + `<svg id="ew-days"></svg>`) +
  `</div><div class="grid2">` +
    card("Исполнители за 30 дней", "Кто ведёт аварийные работы: число вызовов, доля с отключением абонентов, плановая длительность.",
      `<div class="scroll tall">${table(["Исполнитель", "Вызовов", "С отключением", "Медиана, дней", "Сейчас"],
        (W.executors || []).map(x => [esc(org(x.lead)), int(x.n30), pct(x.outage_share), x.median_days == null ? "—" : int(x.median_days), int(x.active)]))}</div>`) +
    card("Районы", "Аварийные работы: действуют сейчас и зарегистрированы за 30 дней.",
      `<div class="scroll tall">${table(["Район", "Сейчас", "За 30 дней"], (W.em_districts || []).map(r => [esc(r[0]), int(r[1]), int(r[2])]))}</div>`) +
  `</div>` +
    (pk ? card("Где работы задевают платную парковку", "Аварийные работы и земляные работы до 120 дней и площадью до 2 га, с запасом 10 м. Длительные ограждения благоустройства не учитываются.",
      `<div class="scroll">${table(["Район", "Мест под работами"], pk.by_district.map(r => [esc(r[0]), int(r[1])]))}</div>`) : "") +
    card("Последние аварийные вызовы", "", `<div class="scroll tall">${table(["Дата", "Район", "Сеть", "Исполнитель", "Отключение", "Место"],
      (W.em_recent || []).map(x => [esc(x.date), esc(x.district), esc(x.net), esc(org(x.lead)), x.outage ? `<span class="pill close">да</span>` : "нет", esc(addr(x.place) || "—")]),
      {left: [0, 1, 2, 3, 5]})}</div>`);
  if (emS.length) lineChart($("em-days"), W.days, emS, {every: 14});
  if (ewS.length) lineChart($("ew-days"), W.days, ewS, {every: 14});
}

/* ---------- журнал ---------- */
function renderLog() {
  const root = $("tab-log");
  const title = Object.fromEntries(DATA.datasets.map(d => [d.id, d]));
  root.innerHTML = card("Синхронизации", "Каждая строка — запуск, в котором набор изменился. Первая синхронизация записывает весь набор как «добавлено».",
      DATA.log.length ? `<div class="scroll tall">${table(["Время (МСК)", "Набор", "Добавлено", "Изменено", "Удалено"],
        DATA.log.map(l => [esc(l.at), `№ ${l.dataset} · ${esc(title[l.dataset]?.title || "")}`, int(l.added), int(l.changed), int(l.removed)]), {left: [0, 1]})}</div>`
        : `<p class="empty">Пока нет ни одной синхронизации.</p>`) +
    card("Источники", "Портал открытых данных Москвы, API apidata.mos.ru. Использование — с указанием источника.",
      table(["Набор", "Поток"], DATA.datasets.map(d => [`<a href="https://data.mos.ru/opendata/${d.id}" target="_blank" rel="noopener">№ ${d.id}</a> · ${esc(d.title)}`, esc(d.stream)]), {left: [0, 1]})) +
    card("Как считается", "",
      `<p class="note">Каждый запуск снимает полный снимок наборов и сравнивает его с журналом: в журнал попадают только новые, изменённые и исчезнувшие записи. ` +
      `История до начала учёта восстановлена по датам внутри записей (выдача лицензии, приём заявок, регистрация вызова); смены статусов видны начиная с первой синхронизации. ` +
      `Если снимок пришёл неполным, запуск прерывается и ничего не записывает.</p>`);
}

const TABS = [["licenses", "Общепит и алкоритейл"], ["lots", "Машино-места"], ["works", "Помехи по адресу"], ["log", "Журнал"]];
function select(key) {
  TABS.forEach(([k]) => { $("tab-" + k).hidden = k !== key; document.querySelector(`[data-tab="${k}"]`).setAttribute("aria-selected", k === key); });
  try { localStorage.setItem("pulse-tab", key); } catch (e) {}
}
function draw() { renderLicenses(); renderLots(); renderWorks(); renderLog(); }
function init() {
  $("sub").textContent = `Открытые данные Москвы · данные на ${DATA.today.split("-").reverse().join(".")} · собрано ${DATA.generated} МСК`;
  $("tabs").innerHTML = TABS.map(([k, t]) => `<button role="tab" data-tab="${k}">${t}</button>`).join("");
  $("tabs").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { select(b.dataset.tab); history.replaceState(null, "", "#" + b.dataset.tab); } });
  draw();
  let start = (location.hash || "").slice(1);
  if (!TABS.some(([k]) => k === start)) { try { start = localStorage.getItem("pulse-tab") || "licenses"; } catch (e) { start = "licenses"; } }
  select(TABS.some(([k]) => k === start) ? start : "licenses");
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
  new MutationObserver(draw).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
}
init();
</script>
</body>
</html>
"""
