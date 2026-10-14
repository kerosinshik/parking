"""Страница «Рынок квартир»: что собрано в журнал рынка и как квартиры на торгах выглядят на его фоне.

Отдельная от дашборда Пульса (`track-report`): канал читает Пульс, Пульс о ней не знает. Внешний вид — общий
(стили берутся из шаблона дашборда), данные — свои: журнал рынка (market.py), квартиры в продаже из журнала
№ 1461 (lots.py) и итоги торгов квартир (track-torgi --types квартира).
"""

from __future__ import annotations

import html
import json
import re
import statistics
from datetime import date
from pathlib import Path

from ..timeutil import now_msk
from ..track.dashboard import TEMPLATE as PULSE_TEMPLATE
from . import lots, market, templates as t
from .content import POLICY


def _ref(obs, metric, district, adm, today, age):
    # с лотами сравниваем только район и округ: средняя по Москве смешивает центр и окраины (она — фоном, city)
    r = market.reference(obs, metric, district, adm, today, age, levels=("район", "округ"))
    return {"value": r["value"], "level": r["level"], "observed": r["observed"].isoformat(), "source": r["source"],
            "url": r.get("url") or None} if r else None


def _med(xs):
    xs = [x for x in xs if x]
    return statistics.median(xs) if xs else None


def collect(con, state_dir, today: date, market_path=None, results_path=None, age=None) -> dict:
    age = age or POLICY["market_max_age_days"]
    obs = market.load(market_path)
    have = lots.load(con, state_dir, today)
    all_results = lots.results(results_path)
    days = POLICY["torgi_days"]
    sold = market.torgi_sold(all_results, today, days)   # свой ориентир: итоги торгов квартир за окно

    def tref(district, adm, exclude=None, address=None):
        r = market.torgi_reference(sold, district, adm, exclude=exclude, address=address)
        return {"value": r["value"], "n": r["n"], "level": r["level"]} if r else None

    flats = []
    if have:
        flats = lots._finish(lots._rows(con, """
            SELECT record_id, lot_id, address, district, adm, space, price, base, discount, scope, end_d, trades_d, site
            FROM ch_sale WHERE kind = 'flat' ORDER BY base"""))
    for f in flats:
        f["okrug"] = market.okrug_key(f["adm"])
        sale = _ref(obs, "sale_m2", f["district"], f["adm"], today, age)
        rent = _ref(obs, "rent_m2_month", f["district"], f["adm"], today, age)
        f["market"], f["rent"] = sale, rent
        if sale and f["space"]:
            f["gap"] = 1 - f["base"] / sale["value"]
            f["gain"] = (sale["value"] - f["base"]) * f["space"]
        if rent and f["price"] and f["space"]:
            f["yield"] = rent["value"] * 12 * f["space"] / f["price"]
        f["torgi"] = tref(f["district"], f["adm"], address=f["address"])
        again = market.torgi_relisted(sold, f["address"], f["space"])
        f["relisted"] = again["date"].isoformat() if again else None
        if f["torgi"]:
            f["gap_t"] = 1 - f["base"] / f["torgi"]["value"]
        f["end_d"] = f["end_d"].isoformat() if f.get("end_d") else None
        f["trades_d"] = f["trades_d"].isoformat() if f.get("trades_d") else None

    def group(key, rows):
        out = {}
        for f in rows:
            out.setdefault(f[key] or "—", []).append(f)
        return out

    districts = []
    for d, rows in sorted(group("district", flats).items()):
        m = rows[0]["market"]
        lot_m2 = _med([r["base"] for r in rows])
        tr = tref(d, rows[0]["adm"])   # ориентир района (без привязки к дому)
        districts.append({"torgi": tr, "gap_t": 1 - lot_m2 / tr["value"] if tr else None, "district": t.district(d), "okrug": rows[0]["okrug"], "n": len(rows), "lot_m2": lot_m2,
                          "min_m2": min(r["base"] for r in rows), "space": _med([r["space"] for r in rows]),
                          "market": m, "rent": rows[0]["rent"], "gap": 1 - lot_m2 / m["value"] if m else None})
    okrugs = []
    for o, rows in group("okrug", flats).items():
        m = _ref(obs, "sale_m2", None, o, today, age)
        lot_m2 = _med([r["base"] for r in rows])
        osold = [x["m2"] for x in sold if x["okrug"] == o and x["m2"] and x["recent"]]
        okrugs.append({"okrug": o, "n": len(rows), "lot_m2": lot_m2, "market": m,
                       "final_m2": _med(osold) if len(osold) >= market.TORGI_MIN else None, "final_n": len(osold),
                       "gap": 1 - lot_m2 / m["value"] if m else None})
    okrugs.sort(key=lambda x: -(x["lot_m2"] or 0))

    results = []
    for r in all_results:
        if r["kind"] != "flat":
            continue
        m2 = r.get("final_price_per_m2") or (r["final_price"] / r["space_m2"] if r.get("final_price") and r.get("space_m2") else None)
        sale = _ref(obs, "sale_m2", r.get("district"), r.get("adm_area"), today, age)
        results.append({"date": (r.get("trades_date") or "")[:10], "address": r["place"], "district": t.district(r.get("district")),
                        "okrug": market.okrug_key(r.get("adm_area")), "status": r.get("status"),
                        "start": r.get("start_price"), "final": r.get("final_price"), "space": r.get("space_m2"),
                        "m2": m2, "ratio": r.get("final_to_start"), "house_type": r.get("house_type"),
                        "market": sale, "gap": 1 - m2 / sale["value"] if sale and m2 else None, "url": r.get("url")})
        tr = tref(r.get("district"), r.get("adm_area"), exclude=r.get("tender_id"), address=r.get("address"))
        results[-1].update(torgi=tr, gap_t=1 - m2 / tr["value"] if tr and m2 else None)
    results.sort(key=lambda r: r["date"], reverse=True)

    journal = [{"observed": o["observed"].isoformat() if o["observed"] else None, "level": o["level"],
                "area": t.district(o["area"]), "segment": o.get("segment"), "rooms": o.get("rooms"),
                "quality": o.get("quality"),
                "metric": market.METRICS.get(o["metric"], (o["metric"],))[0], "value": o["value"],
                "source": o["source"], "url": o.get("url") or None, "added": o.get("added"), "note": o.get("note")}
               for o in reversed(obs)]
    city = []   # фон по Москве: последний период каждого источника и сегмента, все квартиры и по качеству
    for (src, seg), _ in sorted({(o["source"], o.get("segment")): 1 for o in obs if o["level"] == "город"
                                 and o["metric"] == "sale_m2"}.items()):
        rows = [o for o in obs if o["level"] == "город" and o["metric"] == "sale_m2" and o["source"] == src
                and o.get("segment") == seg and o["observed"]]
        last = max(o["observed"] for o in rows)
        city.append({"source": src, "segment": seg, "observed": last.isoformat(), "note": rows[-1].get("note"),
                     "url": rows[-1].get("url") or None,
                     "values": {o.get("quality") or "все": o["value"] for o in rows if o["observed"] == last}})
    cov = {"район": 0, "округ": 0, "город": 0, "нет": 0}
    for d in districts:
        cov[d["market"]["level"] if d["market"] else "нет"] += 1
    return {"today": today.isoformat(), "generated": now_msk().strftime("%d.%m.%Y %H:%M"), "age": age["район"] if isinstance(age, dict) else age, "city": city, "torgi_days": days, "sold_n": sum(1 for x in sold if x["m2"] and x["recent"]),
            "have_lots": have, "flats": flats, "districts": districts, "okrugs": okrugs, "results": results,
            "journal": journal, "coverage": cov,
            "sources": sorted({o["source"] for o in obs}),
            "need": [d["district"] for d in districts if not d["market"] or d["market"]["level"] != "район"]}


def render(con, state_dir, out_path, *, today: date | None = None, market_path=None, results_path=None,
           title: str = "Рынок квартир") -> Path:
    today = today or now_msk().date()
    data = collect(con, state_dir, today, market_path, results_path)
    payload = json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":")).replace("</", "<\\/")
    style = re.search(r"<style>.*?</style>", PULSE_TEMPLATE, re.S).group(0)   # общий вид с дашбордом Пульса
    page = (TEMPLATE.replace("__STYLE__", style).replace("__TITLE__", html.escape(title)).replace("__DATA__", payload))
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
__STYLE__
<style>
.dot-lot { fill: var(--s1); } .dot-mkt { fill: var(--s2); }
.warn { border-left: 3px solid var(--s2); padding-left: 10px; }
td.l { white-space: normal; min-width: 160px; }
</style>
</head>
<body>
<main>
  <h1>__TITLE__</h1>
  <p class="sub" id="sub"></p>
  <nav role="tablist" id="tabs"></nav>
  <section id="tab-overview" role="tabpanel"></section>
  <section id="tab-lots" role="tabpanel" hidden></section>
  <section id="tab-results" role="tabpanel" hidden></section>
  <section id="tab-journal" role="tabpanel" hidden></section>
</main>
<div id="tip" role="tooltip"></div>
<script>
const D = __DATA__;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const nf = new Intl.NumberFormat("ru-RU");
const rub = v => v == null ? "—" : nf.format(Math.round(v)) + " ₽";
const k = v => v == null ? "—" : nf.format(Math.round(v / 1000)) + " тыс.";
const mln = v => v == null ? "—" : (v / 1e6).toLocaleString("ru-RU", {maximumFractionDigits: 2}) + " млн ₽";
const m2 = v => v ? String(Math.round(v * 10) / 10).replace(".", ",") + " м²" : "—";
const pct = v => v == null ? "—" : Math.round(v * 100) + " %";
const gapCell = v => v == null ? "<td>—</td>" : `<td class="${v > 0 ? "pos" : "neg"}">${v > 0 ? "−" : "+"}${Math.abs(Math.round(v * 100))} %</td>`;
const day = s => s ? s.split("-").reverse().join(".") : "—";
const src = m => m ? `${esc(m.source)}, ${day(m.observed)}${m.level !== "район" ? " · по " + (m.level === "округ" ? "округу" : "Москве") : ""}` : "нет данных";
const tip = $("#tip");
function showTip(e, html) { tip.innerHTML = html; tip.style.display = "block";
  tip.style.left = Math.min(e.clientX + 12, innerWidth - 330) + "px"; tip.style.top = (e.clientY + 12) + "px"; }
function hideTip() { tip.style.display = "none"; }

const TABS = [["overview", "Обзор"], ["lots", "Квартиры в продаже"], ["results", "Итоги торгов"], ["journal", "Журнал рынка"]];
$("#tabs").innerHTML = TABS.map(([id, n], i) => `<button role="tab" aria-selected="${i === 0}" data-t="${id}">${n}</button>`).join("");
$("#tabs").onclick = e => { const b = e.target.closest("button"); if (!b) return;
  document.querySelectorAll("#tabs button").forEach(x => x.setAttribute("aria-selected", x === b));
  TABS.forEach(([id]) => $("#tab-" + id).hidden = id !== b.dataset.t); };

const withMkt = D.flats.filter(f => f.market);
$("#sub").textContent = `На ${day(D.today)} · собрано ${D.generated} МСК · рыночный ориентир не старше ${D.age} дней` +
  (D.sources.length ? ` · источники: ${D.sources.join(", ")}` : "");

// ---------------------------------------------------------------- обзор
function overview() {
  const c = D.coverage, below = withMkt.filter(f => f.gap > 0);
  const tiles = [
    ["Квартир в продаже", D.flats.length, "Москва и Новая Москва, № 1461"],
    ["Районов", D.districts.length, `с ориентиром по району — ${c["район"]}`],
    ["Сравнимы с рынком", withMkt.length, withMkt.length ? `ниже рынка района — ${below.length}` : (D.journal.length ? "нет цен по районам и округам" : "журнал рынка пуст")],
    ["Медиана ₽/м² лотов", k(median(D.flats.map(f => f.base))), "стартовая цена"],
    ["Итогов квартир", D.results.length, D.results.length ? `продано за ${D.torgi_days} дней — ${D.sold_n}` : "таблица итогов не задана"],
  ];
  let h = `<div class="tiles">${tiles.map(([a, b, c]) => `<div class="tile"><div class="k">${a}</div><div class="v">${b}</div><div class="h">${c}</div></div>`).join("")}</div>`;
  if (D.city.length) h += `<div class="card"><h2>Фон по Москве</h2><p class="note">Средняя цена м² по всему городу — с конкретной
    квартирой не сравниваем (смешивает центр и окраины), только для общего фона.</p><div class="scroll"><table><thead><tr>
    <th class="l">Сегмент</th><th>Все квартиры</th><th>Типовые</th><th>Улучшенные</th><th>Элитные</th><th class="l">Источник</th></tr></thead><tbody>
    ${D.city.map(c => `<tr><td class="l">${esc(c.segment)}</td><td>${k(c.values["все"])}</td><td>${k(c.values["типовые"])}</td>
      <td>${k(c.values["улучшенные"])}</td><td>${k(c.values["элитные"])}</td>
      <td class="l">${c.url ? `<a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.source)}</a>` : esc(c.source)}, ${esc(c.note || day(c.observed))}</td></tr>`).join("")}
    </tbody></table></div></div>`;
  if (!D.journal.length) h += `<div class="card warn"><h2>Журнал рынка пуст</h2><p class="note">Сравнение с рынком появится после
    <code>parkan market-add --file new.csv</code>. Пока на странице — только данные торгов. Колонки и правила — docs/channel.md, раздел «Рынок».</p></div>`;
  h += `<div class="card"><h2>Цена м² по округам: старт и итоги торгов</h2>
    <p class="note">Тыс. ₽ за м²: медиана стартовой цены квартир в продаже и медиана итоговой цены квартир, проданных за ${D.torgi_days} дней
    (округ без 5 продаж — без точки)${D.okrugs.some(o => o.market) ? "; рынок — ориентир по округу из журнала" : ""}.</p>
    <div class="legend"><span><span class="sw" style="background:var(--s1);height:8px;width:8px;border-radius:50%"></span>в продаже — медиана старта</span>
    <span><span class="sw" style="background:var(--s3);height:8px;width:8px;border-radius:50%"></span>продано — медиана итога</span>
    ${D.okrugs.some(o => o.market) ? `<span><span class="sw" style="background:var(--s2);height:8px;width:8px;border-radius:50%"></span>рынок</span>` : ""}</div>
    <div id="dumb"></div></div>`;
  h += `<div class="card"><h2>По районам</h2>${D.need.length ? `<details class="note"><summary>Что собирать: ${D.need.length} районов без ориентира
    по району</summary>${esc(D.need.join(", "))}</details>` : `<p class="note">Ориентир по району есть для всех районов.</p>`}<div class="scroll tall"><table><thead><tr>
    <th class="l">Район</th><th>Округ</th><th>Лотов</th><th>Медиана м²</th><th>Мин. м²</th><th>Площадь</th><th>Итоги м²</th><th>Старт к итогам</th><th>Рынок м²</th><th>Торги к рынку</th><th>Аренда м²/мес</th><th class="l">Источник</th>
    </tr></thead><tbody>${D.districts.sort((a, b) => b.n - a.n).map(d => `<tr><td class="l">${esc(d.district)}</td><td>${esc(d.okrug || "—")}</td>
    <td>${d.n}</td><td>${k(d.lot_m2)}</td><td>${k(d.min_m2)}</td><td>${d.space ? Math.round(d.space) + " м²" : "—"}</td>
    <td>${d.torgi ? k(d.torgi.value) + (d.torgi.level === "округ" ? " (окр.)" : "") : "—"}</td>${gapCell(d.gap_t)}
    <td>${d.market ? k(d.market.value) : "—"}</td>${gapCell(d.gap)}<td>${d.rent ? rub(d.rent.value) : "—"}</td><td class="l">${src(d.market)}</td></tr>`).join("")}
    </tbody></table></div></div>`;
  $("#tab-overview").innerHTML = h;
  dumbbell($("#dumb"), D.okrugs);
}
function median(xs) { xs = xs.filter(x => x != null).sort((a, b) => a - b); if (!xs.length) return null;
  const m = xs.length >> 1; return xs.length % 2 ? xs[m] : (xs[m - 1] + xs[m]) / 2; }

function dumbbell(el, rows) {
  if (!rows.length) { el.innerHTML = `<p class="empty">Нет квартир в продаже.</p>`; return; }
  const W = 720, L = 70, R = 20, row = 28, H = rows.length * row + 30;
  const vals = rows.flatMap(r => [r.lot_m2, r.final_m2, r.market && r.market.value]).filter(Boolean);
  const max = Math.max(...vals) * 1.08, x = v => L + (W - L - R) * v / max;
  const ticks = []; const step = max > 600000 ? 200000 : 100000; for (let v = 0; v <= max; v += step) ticks.push(v);
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Цена м² по округам">`;
  s += ticks.map(v => `<line x1="${x(v)}" x2="${x(v)}" y1="0" y2="${H - 24}" stroke="var(--grid)"/><text x="${x(v)}" y="${H - 8}" text-anchor="middle">${v / 1000}</text>`).join("");
  rows.forEach((r, i) => {
    const y = i * row + 14, m = r.market && r.market.value;
    s += `<text x="${L - 8}" y="${y + 4}" text-anchor="end">${esc(r.okrug)}</text>`;
    const ends = [r.lot_m2, r.final_m2, m].filter(Boolean);
    if (ends.length > 1) s += `<line x1="${x(Math.min(...ends))}" x2="${x(Math.max(...ends))}" y1="${y}" y2="${y}" stroke="var(--axis)" stroke-width="2"/>`;
    if (r.final_m2) s += `<circle cx="${x(r.final_m2)}" cy="${y}" r="5" fill="var(--s3)" stroke="var(--surface)" stroke-width="2"/>`;
    s += `<circle class="dot-lot" cx="${x(r.lot_m2)}" cy="${y}" r="5" stroke="var(--surface)" stroke-width="2"/>`;
    if (m) s += `<circle class="dot-mkt" cx="${x(m)}" cy="${y}" r="5" stroke="var(--surface)" stroke-width="2"/>`;
    s += `<rect x="0" y="${y - row / 2}" width="${W}" height="${row}" fill="transparent" data-i="${i}"/>`;
  });
  el.innerHTML = s + "</svg>";
  el.querySelectorAll("rect[data-i]").forEach(rc => {
    const r = rows[rc.dataset.i];
    rc.onmousemove = e => showTip(e, `<b>${esc(r.okrug)}</b> · в продаже ${r.n}<br>старт: ${k(r.lot_m2)} ₽/м²<br>итоги: ${r.final_m2 ? k(r.final_m2) + " ₽/м² (продано " + r.final_n + ")" : "мало продаж (" + r.final_n + ")"}<br>рынок: ${r.market ? k(r.market.value) + " ₽/м² (" + src(r.market) + ")" : "нет данных"}` +
      (r.gap != null ? `<br>торги ${r.gap > 0 ? "ниже" : "выше"} рынка на ${Math.abs(Math.round(r.gap * 100))} %` : ""));
    rc.onmouseleave = hideTip;
  });
}

// ---------------------------------------------------------------- квартиры в продаже
function lotsTab() {
  const okrugs = [...new Set(D.flats.map(f => f.okrug).filter(Boolean))].sort();
  $("#tab-lots").innerHTML = `<div class="card"><div class="filters">
    <label>Округ<select id="f-ok"><option value="">все</option>${okrugs.map(o => `<option>${o}</option>`).join("")}</select></label>
    <label>Сортировка<select id="f-sort"><option value="gap_t">дешевле итогов района</option><option value="gap">ниже рынка</option><option value="discount">дешевле лотов района</option><option value="base">цена м²</option><option value="end">срок заявок</option></select></label>
    </div><p class="note">«К лотам района» — против медианы стартовых цен других квартир в продаже; «к итогам района» — против медианы
    итоговых цен квартир, проданных в районе за ${D.torgi_days} дней (меньше 5 продаж — по округу); «к рынку» — против ориентира района из журнала.
    Разница с рынком — до ремонта, налогов и расходов; доходность аренды — валовая.</p>
    <div class="scroll tall"><table><thead><tr><th class="l">Адрес</th><th>Округ</th><th>Площадь</th><th>Цена</th><th>м²</th>
    <th>К лотам района</th><th>К итогам района</th><th>К рынку</th><th>Разница с рынком</th><th>Аренда, год. дох.</th><th>Заявки до</th></tr></thead><tbody id="lots-b"></tbody></table></div></div>`;
  const draw = () => {
    const ok = $("#f-ok").value, by = $("#f-sort").value;
    const key = {gap_t: f => -(f.gap_t ?? -9), gap: f => -(f.gap ?? -9), discount: f => -(f.discount ?? -9), base: f => f.base, end: f => f.end_d || "9"}[by];
    const rows = D.flats.filter(f => !ok || f.okrug === ok).sort((a, b) => key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0);
    $("#lots-b").innerHTML = rows.map(f => `<tr><td class="l">${f.url ? `<a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.place)}</a>` : esc(f.place)}<br><span class="note">${esc((f.district || "").replace(/^район /, ""))}${f.torgi && f.torgi.level === "дом" ? " · есть итоги в этом доме" : ""}${f.relisted ? " · уже не продалась " + day(f.relisted) : ""}</span></td>
      <td>${esc(f.okrug || "—")}</td><td>${m2(f.space)}</td><td>${mln(f.price)}</td><td>${k(f.base)}</td>
      ${gapCell(f.discount)}${gapCell(f.gap_t)}${gapCell(f.gap)}<td>${f.gain > 0 ? mln(f.gain) : "—"}</td><td>${f.yield ? (f.yield * 100).toFixed(1).replace(".", ",") + " %" : "—"}</td><td>${day(f.end_d)}</td></tr>`).join("")
      || `<tr><td colspan="11" class="empty">Нет квартир.</td></tr>`;
  };
  $("#f-ok").onchange = $("#f-sort").onchange = draw;
  if (!D.flats.some(f => f.gap_t != null)) $("#f-sort").value = withMkt.length ? "gap" : "discount";
  draw();
}

// ---------------------------------------------------------------- итоги
function resultsTab() {
  const R = D.results;
  if (!R.length) { $("#tab-results").innerHTML = `<div class="card warn"><h2>Итогов квартир нет</h2><p class="note">Нужна таблица
    <code>track-torgi --types квартира --results results/torgi-kvartiry.csv</code> и ключ <code>--results</code> у market-page.</p></div>`; return; }
  const sold = R.filter(r => r.final), atStart = sold.filter(r => (r.ratio || 0) <= 1.005);
  const by = {}; R.forEach(r => (by[r.status || "—"] = (by[r.status || "—"] || 0) + 1));
  $("#tab-results").innerHTML = `<div class="tiles">
    <div class="tile"><div class="k">Торгов</div><div class="v">${R.length}</div><div class="h">${Object.entries(by).map(([a, b]) => esc(a) + " — " + b).join("; ")}</div></div>
    <div class="tile"><div class="k">Продано</div><div class="v">${sold.length}</div><div class="h">по стартовой цене — ${atStart.length} (${sold.length ? pct(atStart.length / sold.length) : "—"})</div></div>
    <div class="tile"><div class="k">Медиана итога м²</div><div class="v">${k(median(sold.map(r => r.m2)))}</div><div class="h">рост к старту — ×${(median(sold.map(r => r.ratio)) || 0).toFixed(2).replace(".", ",")}</div></div>
    <div class="tile"><div class="k">Дешевле итогов района</div><div class="v">${sold.filter(r => r.gap_t > 0).length}</div><div class="h">из ${sold.filter(r => r.gap_t != null).length} сравнимых</div></div></div>
    <div class="card"><div class="scroll tall"><table><thead><tr><th>Дата</th><th class="l">Адрес</th><th class="l">Статус</th><th>Площадь</th>
    <th>Старт</th><th>Итог</th><th>×</th><th>м²</th><th>К итогам района</th><th class="l">Дом</th></tr></thead><tbody>
    ${R.map(r => `<tr><td>${day(r.date)}</td><td class="l">${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.address)}</a>` : esc(r.address)}<br><span class="note">${esc(r.district)}</span></td>
      <td class="l">${esc(r.status || "—")}</td><td>${m2(r.space)}</td><td>${mln(r.start)}</td><td>${mln(r.final)}</td>
      <td>${r.ratio ? r.ratio.toFixed(2).replace(".", ",") : "—"}</td><td>${k(r.m2)}</td>${gapCell(r.gap_t)}<td class="l">${esc(r.house_type || "")}</td></tr>`).join("")}
    </tbody></table></div></div>`;
}

// ---------------------------------------------------------------- журнал
function journalTab() {
  const J = D.journal;
  $("#tab-journal").innerHTML = `<div class="card"><h2>Наблюдения (${J.length})</h2><p class="note">Свежие сверху. Добавление — <code>parkan market-add</code>;
    строки не правятся, поправка — новая строка.</p>${J.length ? `<div class="scroll tall"><table><thead><tr><th>Дата</th><th class="l">Уровень</th><th class="l">Где</th>
    <th class="l">Сегмент</th><th class="l">Метрика</th><th>Значение</th><th class="l">Источник</th><th>Добавлено</th><th class="l">Заметка</th></tr></thead><tbody>
    ${J.map(o => `<tr><td>${day(o.observed)}</td><td class="l">${esc(o.level)}</td><td class="l">${esc(o.area)}</td><td class="l">${esc(o.segment)}${o.rooms && o.rooms !== "все" ? ", " + esc(o.rooms) : ""}${o.quality && o.quality !== "все" ? ", " + esc(o.quality) : ""}</td>
      <td class="l">${esc(o.metric)}</td><td>${rub(o.value)}</td><td class="l">${o.url ? `<a href="${esc(o.url)}" target="_blank" rel="noopener">${esc(o.source)}</a>` : esc(o.source)}</td>
      <td>${day(o.added)}</td><td class="l">${esc(o.note || "")}</td></tr>`).join("")}</tbody></table></div>` : `<p class="empty">Пока пусто.</p>`}</div>`;
}

overview(); lotsTab(); resultsTab(); journalTab();
</script>
</body>
</html>
"""
