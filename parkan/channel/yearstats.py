"""Аналитика года торгов машино-местами для статьи (vc.ru и т. п.): факты и графики 1600×900.

Считает по таблице итогов track-torgi (results/torgi-mashinomesta.csv) — только чтение, Пульс не трогает.
Главная мысль статьи — будет ли торг, решает дом, а не сезон, метро или уровень паркинга, поэтому
почти каждый показатель проверяется внутри домов: дом — адрес до корпуса включительно.

Определения:
  продано         «Признаны состоявшимися» и «Единственный участник»
  по старту       итог равен стартовой цене, среди продаж с известным итогом
  доля продаж     продано / (продано + не состоялись), без отменённых и ждущих итогов
  без выбросов    суммы денег — без ставок от ×20 к старту (их не выкупили, см. статью о 2,13 млрд)

Факты — JSON (compute → facts.json), картинки — render(facts, out_dir) → vc-1-cover.png … vc-8-myths.png.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

from . import html_cards as hc
from .html_cards import BG, LIGHT, LIME, MUTED, RULE, SUB

SOLD = ("Признаны состоявшимися", "Единственный участник")
NOSALE = "Признаны несостоявшимися"
OKRUG_SHORT = {
    "Центральный": "ЦАО", "Северный": "САО", "Северо-Восточный": "СВАО", "Восточный": "ВАО", "Юго-Восточный": "ЮВАО",
    "Южный": "ЮАО", "Юго-Западный": "ЮЗАО", "Западный": "ЗАО", "Северо-Западный": "СЗАО", "Зеленоградский": "ЗелАО",
    "Новомосковский": "НАО", "Троицкий": "ТАО",
}
MONTHS = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")

# ---------------------------------------------------------------- расчёт

_LOAD = r"""
create or replace table m as
select *, try_cast(start_price as double) sp, try_cast(final_price as double) fp,
  try_cast(final_to_start as double) r, try_cast(trades_date as date) d,
  trim(regexp_extract(display_address, '([^,]+, д\. [^,]+(?:, корп\. [^,]+)?)', 1)) house,
  trim(replace(adm_area, ' административный округ', '')) ao,
  try_cast(regexp_extract(subway, '\((\d+) мин', 1) as int) walk,
  try_cast(regexp_extract(display_address, '(?:[Ээ]таж|[Пп]одвал)\s*№?\s*(-?\d+)', 1) as int) lvl,
  status in ('Признаны состоявшимися', 'Единственный участник') sold,
  status in ('Признаны состоявшимися', 'Единственный участник') and try_cast(final_to_start as double) is not null known
from read_csv_auto(?, all_varchar=true)
"""


def _rows(con, sql: str, *args) -> list[dict]:
    cur = con.execute(sql, list(args))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _one(con, sql: str, *args) -> dict:
    return _rows(con, sql, *args)[0]


def compute(results_path, min_house_sales: int = 10) -> dict:
    """Факты для статьи и графиков по таблице итогов машино-мест."""
    import duckdb
    con = duckdb.connect()
    con.execute(_LOAD, [str(results_path)])
    k = min_house_sales

    tot = _one(con, """select count(*) lots, count(*) filter(where sold) sold, count(*) filter(where status=?) nosale,
        count(*) filter(where status='Отменены') cancelled, count(*) filter(where known) known,
        count(*) filter(where known and r=1) at_start, count(distinct house) filter(where house<>'') houses,
        sum(coalesce(fp, sp)) filter(where sold and coalesce(r, 1) < 20) money,
        median(coalesce(fp, sp)) filter(where sold and coalesce(r, 1) < 20) median_price,
        count(distinct cadastral) filter(where cadastral<>'') places,
        strftime(min(d), '%Y-%m-%d') date_from, strftime(max(d), '%Y-%m-%d') date_to from m""", NOSALE)

    # тренд: как выглядит доля продаж по старту и что остаётся внутри тех же домов
    trend = _rows(con, f"""
      with s as (select * from m where known and house<>''),
      h as (select house from s group by 1 having count(distinct strftime(d, '%Y-%m')) >= 2 and count(*) >= {k}),
      x as (select s.*, (r=1)::int a, avg((r=1)::int) over (partition by s.house) ha from s join h using(house))
      select strftime(d, '%Y-%m') AS month, count(*) n, avg(a) raw, (select avg(a) from x) + avg(a - ha) in_house
      from x group by 1 order by 1""")
    all_trend = _rows(con, """select strftime(d, '%Y-%m') AS month, count(*) n, avg((r=1)::int) raw
                              from m where known group by 1 order by 1""")

    # дома: распределение по доле продаж по старту и что отличает горячие
    houses = _rows(con, f"""
      with s as (select *, median(sp) over (partition by ao) ao_sp from m
                 where known and house<>'' and trades_form<>'публичное предложение')
      select house, any_value(ao) ao, count(*) n, avg((r=1)::int) at_start, median(r) med_ratio,
        median(sp) med_start, median(fp) med_final, median(sp/ao_sp) start_rel, median(fp/ao_sp) final_rel,
        median(walk) walk, min(strftime(d, '%Y-%m-%d')) first_trades
      from s group by 1 having count(*) >= {k} order by at_start, n desc""")
    buckets = [0] * 5
    for h in houses:
        buckets[min(int(h["at_start"] * 5), 4)] += 1
    groups = []
    for name, lo, hi in (("горячие", 0, 0.2), ("средние", 0.2, 0.8), ("без торга", 0.8, 1.01)):
        g = [h for h in houses if lo <= h["at_start"] < hi]
        groups.append({"group": name, "houses": len(g), "sales": sum(h["n"] for h in g),
                       "start_rel": _median([h["start_rel"] for h in g]), "final_rel": _median([h["final_rel"] for h in g])})

    # размер дома: сколько мест продаётся
    size = _rows(con, """
      with h as (select house, count(*) lots, count(*) filter(where sold) s, count(*) filter(where status=?) ns
                 from m where house<>'' group by 1 having count(*) >= 10)
      select case when lots < 40 then 'до 40' when lots < 100 then '40–99' when lots < 200 then '100–199' else '200+' end size,
        min(lots) lo, count(*) houses, sum(lots) lots, avg(s / nullif(s + ns, 0)) sold_share
      from h group by 1 order by lo""", NOSALE)
    cold = _rows(con, """
      select house, any_value(ao) ao, count(*) lots, count(*) filter(where sold) / nullif(count(*) filter(where sold or status=?), 0) sold_share
      from m where house<>'' group by 1 having count(*) >= 100 order by sold_share limit 6""", NOSALE)

    # судьба мест: с какой попытки продано и что бывает с непроданными
    funnel = _one(con, """
      with x as (select cadastral, list(status order by d) st from m where cadastral<>'' group by 1),
      y as (select st, list_position(list_transform(st, s -> s in ('Признаны состоявшимися', 'Единственный участник')), true) fs from x)
      select count(*) places, count(*) filter(where fs = 1) first_try, count(*) filter(where fs > 1) later,
        count(*) filter(where fs is null and list_contains(st, 'Прием заявок завершен')) pending,
        count(*) filter(where fs is null and not list_contains(st, 'Прием заявок завершен')) unsold from y""")
    relist = _one(con, """
      with x as (select status, sp, d, lead(status) over w ns, lead(sp) over w nsp, lead(d) over w nd
                 from m where cadastral<>'' window w as (partition by cadastral order by d))
      select count(*) nosale, count(ns) relisted, avg((abs(nsp/sp - 1) <= 0.01)::int) filter(where ns is not null) same,
        avg((nsp > sp*1.01)::int) filter(where ns is not null) dearer, avg((nsp < sp*0.99)::int) filter(where ns is not null) cheaper,
        avg((ns in ('Признаны состоявшимися', 'Единственный участник'))::int) filter(where ns in
            ('Признаны состоявшимися', 'Единственный участник', 'Признаны несостоявшимися')) sold_next,
        median(nd - d) filter(where ns is not null) AS days
      from x where status = ?""", NOSALE)
    cancel = _one(con, """
      with x as (select status, sp, lead(sp) over w nsp from m where cadastral<>'' window w as (partition by cadastral order by d))
      select count(nsp) relisted, avg((nsp > sp*1.01)::int) filter(where nsp is not null) dearer,
        median(nsp/sp) filter(where nsp is not null) ratio from x where status='Отменены'""")

    okrugs = _rows(con, """
      select ao, count(*) lots, count(*) filter(where sold) / nullif(count(*) filter(where sold or status=?), 0) sold_share,
        avg((r=1)::int) filter(where known) at_start, median(fp) filter(where sold and coalesce(r, 1) < 20) med_final
      from m group by 1 having count(*) >= 100 order by lots desc""", NOSALE)

    # гипотезы, которые не держатся внутри домов
    metro = _rows(con, f"""
      with h as (select house, median(walk) <= 10 near, avg((r=1)::int) a from m
                 where known and house<>'' and walk is not null group by 1 having count(*) >= {k})
      select l.near, l.lot_at_start, median(h.a) house_at_start, count(h.house) houses
      from (select walk <= 10 near, avg((r=1)::int) lot_at_start from m where known and walk is not null group by 1) l
      join h using(near) group by 1, 2 order by 1 desc""")
    level = _rows(con, """
      with s as (select * from m where known and house<>'' and lvl is not null),
      hh as (select house from s group by 1 having count(distinct lvl) >= 2),
      x as (select s.*, avg((r=1)::int) over (partition by s.house) ha from s join hh using(house))
      select l.lvl, count(*) n, avg((l.r=1)::int) lot_at_start,
        (select avg((r=1)::int - ha) from x where x.lvl = l.lvl) in_house,
        (select count(distinct house) from x where x.lvl = l.lvl) houses
      from m l where l.known and l.lvl in (-2, 0) group by 1 order by 1""")
    monday = _rows(con, """
      with s as (select *, avg((r=1)::int) over (partition by house) ha from m where known and house<>'')
      select dayofweek(d) = 1 monday, count(*) n, avg((r=1)::int) lot_at_start, avg((r=1)::int - ha) in_house
      from s group by 1 order by 1""")

    return {
        "source": "итоги торгов машино-местами на torgi.mos.ru (набор № 1461 «Торги городским имуществом»), "
                  f"торги {tot['date_from']} — {tot['date_to']}",
        "totals": tot, "trend": trend, "all_trend": all_trend,
        "houses": {"count": len(houses), "min_sales": k, "buckets": buckets, "groups": groups,
                   "hot": [h for h in houses if h["at_start"] < 0.2][:8],
                   "calm": sorted([h for h in houses if h["at_start"] >= 0.95], key=lambda h: -h["n"])[:8]},
        "size": size, "cold": cold, "funnel": funnel, "relist": relist, "cancel": cancel,
        "okrugs": okrugs, "metro": metro, "level": level, "monday": monday,
    }


def _median(xs: list[float]) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def save_facts(facts: dict, path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(facts, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------- форматирование

def num(x: float) -> str:
    """15496 → «15 496» (неразрывный пробел)."""
    return f"{round(x):,}".replace(",", " ")


def pct(x: float) -> str:
    return f"{round(x * 100)} %"


def dec(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}f}".replace(".", ",")


def month_label(ym: str) -> str:
    y, mo = ym.split("-")
    return MONTHS[int(mo) - 1] + (" " + y[2:] if mo == "01" or ym.endswith("-10") and y == "2025" else "")


# ---------------------------------------------------------------- картинки

VW, VH = 1600, 900

CSS = f"""
*{{box-sizing:border-box}} html,body{{margin:0;background:{BG}}}
.vc{{width:{VW}px;height:{VH}px;background:{BG};position:relative;overflow:hidden;font-family:Onest,sans-serif;
  padding:64px 96px 60px;display:flex;flex-direction:column;color:{LIGHT}}}
.top{{display:flex;justify-content:space-between;align-items:center}}
.kicker{{background:{LIME};color:{BG};font:700 28px Onest;letter-spacing:.04em;padding:10px 24px;border-radius:40px}}
.brand{{font:700 26px Unbounded;color:{LIGHT};letter-spacing:.06em}}
.title{{font:700 50px/1.12 Unbounded;color:{LIGHT};letter-spacing:-.02em;margin-top:34px;text-wrap:balance}}
.title em{{font-style:normal;color:{LIME}}}
.sub{{font:500 28px/1.3 Onest;color:{MUTED};margin-top:14px}}
.chart{{flex:1;min-height:0;margin-top:22px;display:flex}}
.chart svg{{display:block;width:100%;height:100%;overflow:visible}}
.src{{font:500 22px Onest;color:{MUTED};margin-top:10px}}
.kpis{{margin-top:auto;display:grid;grid-template-columns:repeat(4,1fr);border-top:2px solid {RULE}}}
.kpi{{padding:34px 28px 0 0}}
.kpi b{{display:block;font:800 74px/1 Unbounded;color:{LIME};letter-spacing:-.04em;white-space:nowrap}}
.kpi span{{display:block;font:500 28px/1.25 Onest;color:{SUB};margin-top:16px}}
.big{{font:800 92px/1.05 Unbounded;color:{LIGHT};letter-spacing:-.04em;margin-top:56px}}
.big em{{font-style:normal;color:{LIME}}}
.myths{{margin-top:auto;display:flex;flex-direction:column}}
.myth{{display:grid;grid-template-columns:380px 1fr 1fr;column-gap:40px;padding:26px 0;border-top:2px solid {RULE};align-items:start}}
.myth .k{{font:700 34px/1.2 Onest;color:{LIGHT}}} .myth .k small{{letter-spacing:.06em}}
.myth .seems,.myth .is{{font:500 27px/1.3 Onest;color:{SUB}}}
.myth .seems b{{color:{LIGHT}}} .myth .is b{{color:{LIME}}}
.myth small{{display:block;font:600 22px Onest;color:{MUTED};letter-spacing:.06em;margin-bottom:8px}}
.side{{display:flex;flex-direction:column;justify-content:center;gap:34px;width:430px;flex:none;padding-left:40px}}
.side b{{display:block;font:800 64px/1 Unbounded;color:{LIME};letter-spacing:-.03em}}
.side span{{display:block;font:500 26px/1.25 Onest;color:{SUB};margin-top:10px}}
"""

def _t(x, y, text, size=26, color=MUTED, weight=500, anchor="start", family="Onest") -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" font-weight="{weight}" '
            f'fill="{color}" text-anchor="{anchor}">{escape(str(text))}</text>')


def _page(kicker: str, body: str) -> str:
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{hc._fonts_css()}{CSS}</style></head>'
            f'<body><div class="vc"><div class="top"><div class="kicker">{escape(kicker)}</div>'
            f'<div class="brand">ПОПУТНЫЙ МЕТР</div></div>{body}</div></body></html>')


def _svg(w: int, h: int, inner: str, box: str = "chart") -> str:
    return f'<div class="{box}"><svg viewBox="0 0 {w} {h}" preserveAspectRatio="xMinYMin meet">{inner}</svg></div>'


def vc_cover(f: dict) -> str:
    t = f["totals"]
    share = t["at_start"] / t["known"]
    return _page("ГОД ТОРГОВ", f"""
  <div class="big">{num(t["lots"])} торгов<br><em>машино-местами</em> за&nbsp;год</div>
  <div class="sub" style="margin-top:22px">Москва, торги с {_ru_date(t["date_from"])} по {_ru_date(t["date_to"])}</div>
  <div class="kpis">
    <div class="kpi"><b>{num(t["sold"])}</b><span>мест продано</span></div>
    <div class="kpi"><b>{dec(t["money"] / 1e9)}<small style="font-size:40px"> млрд ₽</small></b><span>сумма итогов торгов, не меньше</span></div>
    <div class="kpi"><b>{dec(t["median_price"] / 1e6, 2)}<small style="font-size:40px"> млн ₽</small></b><span>медианная цена места</span></div>
    <div class="kpi"><b>{pct(share)}</b><span>продаж — ровно по стартовой цене</span></div>
  </div>""")


def _ru_date(iso: str) -> str:
    y, mo, d = iso.split("-")
    names = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
             "ноября", "декабря")
    return f"{int(d)} {names[int(mo) - 1]} {y}"


def _axes_pct(x0, y0, w, h, ticks=(0, .25, .5, .75, 1)) -> str:
    out = []
    for v in ticks:
        y = y0 + h - v * h
        out.append(f'<line x1="{x0}" x2="{x0 + w}" y1="{y:.1f}" y2="{y:.1f}" stroke="{RULE}" stroke-width="2"/>')
        out.append(_t(x0 - 18, y + 9, f"{round(v * 100)} %", 24, anchor="end"))
    return "".join(out)


def vc_trend(f: dict) -> str:
    # пунктир — все продажи месяца, лайм — те же месяцы с поправкой на дом
    raw = {r["month"]: r["raw"] for r in f["all_trend"]}
    rows = [dict(r, raw=raw.get(r["month"], r["raw"])) for r in f["trend"]]
    W, H, x0, y0, w, h = 1408, 560, 90, 20, 1080, 470
    n = len(rows)
    xs = [x0 + i * w / (n - 1) for i in range(n)]

    def line(key, color, width, dash=""):
        pts = " ".join(f"{x:.1f},{y0 + h - r[key] * h:.1f}" for x, r in zip(xs, rows))
        dots = "".join(f'<circle cx="{x:.1f}" cy="{y0 + h - r[key] * h:.1f}" r="7" fill="{color}"/>' for x, r in zip(xs, rows))
        return (f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linejoin="round" '
                f'stroke-linecap="round" {dash}/>{dots}')
    labels = "".join(_t(x, y0 + h + 44, month_label(r["month"]), 23, anchor="middle") for x, r in zip(xs, rows))
    last = rows[-1]
    first = rows[0]
    ann = (_t(xs[-1] + 26, y0 + h - last["raw"] * h + 4, f"{pct(last['raw'])}", 34, SUB, 700, family="Unbounded")
           + _t(xs[-1] + 26, y0 + h - last["raw"] * h + 36, "без поправки", 24, MUTED)
           + _t(xs[-1] + 26, y0 + h - last["in_house"] * h + 30, f"{pct(last['in_house'])}", 34, LIME, 700, family="Unbounded")
           + _t(xs[-1] + 26, y0 + h - last["in_house"] * h + 62, "в тех же домах", 24, LIME)
           + _t(xs[0], y0 + h - first["raw"] * h - 22, pct(first["raw"]), 28, SUB, 700, anchor="middle", family="Unbounded"))
    inner = (_axes_pct(x0, y0, w, h) + line("raw", SUB, 5, 'stroke-dasharray="2 14"') + line("in_house", LIME, 7)
             + labels + ann)
    return _page("МИФ ГОДА", f"""
  <div class="title">Торговаться перестали? <em>Нет</em> — просто сменились дома</div>
  <div class="sub">Доля продаж по стартовой цене по месяцам. Пунктир — как есть, лайм — с поправкой на дом
  (каждый дом сравнивается сам с собой)</div>
  {_svg(W, H, inner)}""")


def vc_houses(f: dict) -> str:
    hs = f["houses"]
    b = hs["buckets"]
    W, H, x0, y0, w, h = 1408, 520, 40, 40, 1328, 400
    mx = max(b)
    bw = w / 5
    labels = ("0–20 %", "20–40 %", "40–60 %", "60–80 %", "80–100 %")
    bars = []
    for i, v in enumerate(b):
        bh = v / mx * h
        x = x0 + i * bw + 22
        color = LIME if i in (0, 4) else "#3A7A60"
        bars.append(f'<rect x="{x:.1f}" y="{y0 + h - bh:.1f}" width="{bw - 44:.1f}" height="{bh:.1f}" rx="10" fill="{color}"/>')
        bars.append(_t(x + (bw - 44) / 2, y0 + h - bh - 18, v, 44, LIGHT, 800, "middle", "Unbounded"))
        bars.append(_t(x + (bw - 44) / 2, y0 + h + 44, labels[i], 26, SUB, 600, "middle"))
    bars.append(_t(x0 + 22, y0 + h + 84, "← торгуются почти всегда", 24, MUTED))
    bars.append(_t(x0 + w - 22, y0 + h + 84, "почти всегда по старту →", 24, MUTED, anchor="end"))
    return _page("ДОМА", f"""
  <div class="title">Дома делятся на два лагеря: <em>за места бьются</em> или берут по старту</div>
  <div class="sub">{hs["count"]} домов, где продано от {hs["min_sales"]} мест: сколько домов с такой долей продаж по стартовой цене</div>
  {_svg(W, H, "".join(bars))}""")


def vc_hot(f: dict) -> str:
    gs = f["houses"]["groups"]
    W, H, x0, w = 1408, 540, 330, 1000
    lo, hi = 0.6, 1.5

    def X(v):
        return x0 + (v - lo) / (hi - lo) * w
    out = []
    for v in (0.6, 0.8, 1.0, 1.2, 1.4):
        x = X(v)
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="30" y2="460" stroke="{RULE}" stroke-width="{4 if v == 1 else 2}"/>')
        out.append(_t(x, 500, f"{round(v * 100)} %", 24, LIGHT if v == 1 else MUTED, 700 if v == 1 else 500, "middle"))
    out.append(_t(X(1.0), 534, "медианный старт по округу", 22, MUTED, anchor="middle"))
    for i, g in enumerate(gs):
        y = 95 + i * 150
        s, e = g["start_rel"], g["final_rel"]
        hot = i == 0
        col = LIME if hot else SUB
        out.append(_t(0, y - 4, g["group"], 34, LIGHT, 700))
        out.append(_t(0, y + 32, f"{g['houses']} домов, {num(g['sales'])} продаж", 23, MUTED))
        out.append(f'<line x1="{X(s):.1f}" x2="{X(e):.1f}" y1="{y:.1f}" y2="{y:.1f}" stroke="{col}" stroke-width="10" stroke-linecap="round"/>')
        out.append(f'<circle cx="{X(s):.1f}" cy="{y:.1f}" r="16" fill="{BG}" stroke="{col}" stroke-width="6"/>')
        out.append(f'<circle cx="{X(e):.1f}" cy="{y:.1f}" r="16" fill="{col}"/>')
        if abs(e - s) > 0.06:
            out.append(_t(X(s), y - 34, f"старт {round(s * 100)} %", 24, MUTED, 600, "middle"))
            out.append(_t(X(e), y - 34, f"итог {round(e * 100)} %", 26, col, 700, "middle"))
        else:
            out.append(_t(X(e) + 34, y + 9, f"старт ≈ итог ≈ {round(e * 100)} %", 24, MUTED, 600))
    return _page("ГДЕ ТОРГ", f"""
  <div class="title">Торг там, где старт <em>ниже обычного для округа</em> — и итог уходит выше</div>
  <div class="sub">Медиана стартовой и итоговой цены места в доме, в % от медианного старта по округу.
  Горячие — меньше 20 % продаж по старту, без торга — от 80 %</div>
  {_svg(W, H, "".join(out))}""")


def vc_size(f: dict) -> str:
    rows = f["size"]
    W, H, x0, y0, w, h = 1408, 520, 90, 30, 1240, 400
    bw = w / len(rows)
    out = [_axes_pct(x0, y0, w, h, (0, .5, 1))]
    for i, r in enumerate(rows):
        bh = r["sold_share"] * h
        x = x0 + i * bw + 40
        col = LIME if i == len(rows) - 1 else "#3A7A60"
        out.append(f'<rect x="{x:.1f}" y="{y0 + h - bh:.1f}" width="{bw - 80:.1f}" height="{bh:.1f}" rx="10" fill="{col}"/>')
        out.append(_t(x + (bw - 80) / 2, y0 + h - bh - 20, pct(r["sold_share"]), 46, LIGHT, 800, "middle", "Unbounded"))
        out.append(_t(x + (bw - 80) / 2, y0 + h + 44, f"{r['size']} мест в доме", 26, SUB, 600, "middle"))
        out.append(_t(x + (bw - 80) / 2, y0 + h + 78, f"{r['houses']} домов", 22, MUTED, 500, "middle"))
    return _page("БОЛЬШИЕ ПАРКИНГИ", f"""
  <div class="title">Чем больше мест выставили в доме, <em>тем хуже они продаются</em></div>
  <div class="sub">Доля торгов, где место продано (без отменённых), по числу лотов в доме за год</div>
  {_svg(W, H, "".join(out))}""")


def vc_funnel(f: dict) -> str:
    fu, rl = f["funnel"], f["relist"]
    parts = (("first_try", "с первой попытки", LIME), ("later", "со второй и дальше", "#8FBF3A"),
             ("unsold", "не продано", "#3A7A60"), ("pending", "ждут итогов", RULE))
    total = sum(fu[k] for k, *_ in parts)
    W, H, w = 980, 520, 980
    x = 0.0
    out = []
    for i, (key, name, col) in enumerate(parts):
        bw = fu[key] / total * w
        out.append(f'<rect x="{x:.1f}" y="80" width="{max(bw - 4, 2):.1f}" height="150" rx="8" fill="{col}"/>')
        ly = 290 + (i % 2) * 110 if bw < 160 else 290
        if bw >= 160:
            out.append(_t(x + 8, ly, num(fu[key]), 40, LIGHT, 800, family="Unbounded"))
            out.append(_t(x + 8, ly + 34, name, 24, SUB, 600))
        else:
            out.append(f'<line x1="{x + bw / 2:.1f}" x2="{x + bw / 2:.1f}" y1="236" y2="{ly - 40}" stroke="{MUTED}" stroke-width="2"/>')
            out.append(_t(x + bw - 4, ly, num(fu[key]), 40, LIGHT, 800, "end", "Unbounded"))
            out.append(_t(x + bw - 4, ly + 34, name, 24, SUB, 600, "end"))
        x += bw
    out.append(_t(0, 40, f"{num(total)} мест, выставленных за год", 28, LIGHT, 700))
    side = f"""<div class="side">
      <div><b>{pct(rl["same"])}</b><span>непроданных мест выставляют снова по той же цене, через ~{round(rl["days"])} дней</span></div>
      <div><b>{dec(rl["cheaper"] * 100)}&nbsp;%</b><span>— дешевле. Скидок город почти не делает</span></div>
      <div><b>{pct(rl["sold_next"])}</b><span>продаются со следующей попытки</span></div></div>"""
    return _page("СУДЬБА МЕСТА", f"""
  <div class="title">Не продалось — <em>выставят снова, по той же цене</em></div>
  <div style="flex:1;display:flex;min-height:0;margin-top:30px">
    {_svg(W, H, "".join(out), "chart")}{side}</div>""")


def vc_okrugs(f: dict) -> str:
    rows = [r for r in f["okrugs"] if r["at_start"] is not None]
    W, H, x0, y0, w, h = 1408, 560, 110, 20, 1220, 470
    xl, yl = 0.4, 0.2   # оси начинаются там, где есть точки

    def X(v):
        return x0 + (v - xl) / (1 - xl) * w

    def Y(v):
        return y0 + h - (v - yl) / (1 - yl) * h
    out = []
    for v in (0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
        out.append(f'<line x1="{X(v):.1f}" x2="{X(v):.1f}" y1="{y0}" y2="{y0 + h}" stroke="{RULE}" stroke-width="2"/>')
        out.append(_t(X(v), y0 + h + 36, f"{round(v * 100)} %", 22, MUTED, anchor="middle"))
    for v in (0.2, 0.4, 0.6, 0.8, 1.0):
        out.append(f'<line x1="{x0}" x2="{x0 + w}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="{RULE}" stroke-width="2"/>')
        out.append(_t(x0 - 16, Y(v) + 8, f"{round(v * 100)} %", 22, MUTED, anchor="end"))
    out.append(_t(x0 + w, y0 + h + 72, "доля торгов, где место продано →", 24, SUB, 600, "end"))
    out.append(f'<text transform="translate({x0 - 82},{y0}) rotate(-90)" font-family="Onest" font-size="24" font-weight="600" '
               f'fill="{SUB}" text-anchor="end">продано по стартовой цене →</text>')
    mx = max(r["lots"] for r in rows)
    pts = [(X(r["sold_share"]), Y(r["at_start"]), 10 + 26 * (r["lots"] / mx) ** 0.5, r) for r in rows]
    for x, y, rad, _ in pts:
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rad:.1f}" fill="{LIME}" fill-opacity=".9" stroke="{BG}" stroke-width="3"/>')
    boxes = [(x - rad, y - rad, x + rad, y + rad) for x, y, rad, _ in pts]
    for x, y, rad, r in sorted(pts, key=lambda p: -p[2]):
        name = OKRUG_SHORT.get(r["ao"], r["ao"])
        tw = 17 * len(name)
        for lx, ly, anchor in ((x + rad + 10, y + 9, "start"), (x - rad - 10, y + 9, "end"),
                               (x, y - rad - 12, "middle"), (x, y + rad + 30, "middle")):
            left = lx if anchor == "start" else lx - tw if anchor == "end" else lx - tw / 2
            box = (left, ly - 24, left + tw, ly + 4)
            if not any(box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3] for b in boxes):
                break
        boxes.append(box)
        out.append(_t(lx, ly, name, 26, LIGHT, 700, anchor))
    return _page("ОКРУГА", f"""
  <div class="title">Где места берут без торга, <em>а где торгуются</em></div>
  <div class="sub">Каждый круг — округ, размер — число торгов за год</div>
  {_svg(W, H, "".join(out))}""")


def vc_myths(f: dict) -> str:
    me = {r["near"]: r for r in f["metro"]}
    lv, mo = {r["lvl"]: r for r in f["level"]}, {r["monday"]: r for r in f["monday"]}
    pp = lambda v: f"{abs(round(v * 100))}\u00a0п.\u00a0п."  # noqa: E731
    rows = [
        ("Метро в 10 минутах", "работает, но слабее",
         f"по старту продают <b>{pct(me[True]['lot_at_start'])}</b> мест у метро и <b>{pct(me[False]['lot_at_start'])}</b> дальше",
         f"в типичном доме у метро — <b>{pct(me[True]['house_at_start'])}</b>, вдали — <b>{pct(me[False]['house_at_start'])}</b>. "
         f"Остальное — пара огромных горячих домов у метро"),
        ("Нижний уровень паркинга", "не работает",
         f"на −2 этаже по старту <b>{pct(lv[-2]['lot_at_start'])}</b>, на нулевом — <b>{pct(lv[0]['lot_at_start'])}</b>",
         f"внутри одного дома разница <b>{pp(lv[-2]['in_house'] - lv[0]['in_house'])}</b>: −2 этаж просто чаще есть в горячих домах"),
        ("Торги в понедельник", "не работает",
         f"по понедельникам по старту <b>{pct(mo[True]['lot_at_start'])}</b>, в другие дни — <b>{pct(mo[False]['lot_at_start'])}</b>",
         f"внутри дома разница <b>{pp(mo[True]['in_house'] - mo[False]['in_house'])}</b>: на понедельник чаще ставят тихие дома"),
    ]
    body = "".join(f'<div class="myth"><div class="k">{escape(k)}<small style="margin-top:10px;color:{LIME if v.startswith("работает") else MUTED}">'
                   f'{escape(v.upper())}</small></div><div class="seems"><small>КАЖЕТСЯ</small>{a}</div>'
                   f'<div class="is"><small>ЕСЛИ СРАВНИВАТЬ ДОМА</small>{b}</div></div>' for k, v, a, b in rows)
    return _page("ПРОВЕРИЛИ", f"""
  <div class="title">Метро, этаж, день торгов: <em>что остаётся</em>, если смотреть на дом</div>
  <div class="myths">{body}</div>""")


VC = (("vc-1-cover", vc_cover), ("vc-2-trend", vc_trend), ("vc-3-houses", vc_houses), ("vc-4-hot", vc_hot),
      ("vc-5-size", vc_size), ("vc-6-funnel", vc_funnel), ("vc-7-okrugs", vc_okrugs), ("vc-8-myths", vc_myths))


def render(facts: dict, out_dir, only: tuple[str, ...] = ()) -> list[Path]:
    out = Path(out_dir)
    return [hc.render_html(make(facts), out / f"{name}.png", size=(VW, VH))
            for name, make in VC if not only or name in only]
