"""Итоги торгов машино-мест со страниц лотов torgi.mos.ru.

В наборе № 1461 после торгов лот просто «снимается с публикации»: состоялись торги или нет
и за сколько продано место, там не видно. Это есть только на странице лота — сервер отдаёт
данные лота внутри HTML (блок __NUXT_DATA__). Страницы лотов открыты для обхода в robots.txt,
закрыт только внутренний API (/api/), поэтому берём именно страницы и с паузой между запросами.

Таблица итогов — CSV с одной строкой на лот. Каждый запуск дописывает лоты, у которых прошла
дата торгов, и перепроверяет те, у которых итог ещё не опубликован.
"""

from __future__ import annotations

import csv
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from .store import current_sql

LOT_URL = "https://torgi.mos.ru/tender/{}"

FIELDS = [
    "tender_id", "trades_date", "adm_area", "district", "address", "display_address", "lat", "lon", "subway",
    "space_m2", "house_type", "floors_in_house", "floor", "placement", "cadastral",
    "start_price", "start_price_per_m2", "deposit", "auction_step", "procedure_form", "price_kind", "trades_form",
    "status", "final_price", "final_price_per_m2", "final_to_start",
    "apps_from", "apps_to", "trades_at", "results_at", "url", "torgi_gov_url", "auction_protocol_url",
    "checked_at",
]

SOLD = ("Признаны состоявшимися", "Единственный участник")
FINAL = SOLD + ("Признаны несостоявшимися", "Отменены")   # дальше статус не меняется
MISSING = "Страница лота недоступна"


class PageError(RuntimeError):
    pass


def page_transport(url: str, timeout: float = 30.0) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "parkan/0.1 (open data tracker)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def _num(s) -> float | None:
    if s in (None, "", "Не указано", "-"):
        return None
    s = re.sub(r"[^\d,.\-]", "", str(s)).replace(",", ".").strip(".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_page(html: str) -> dict:
    """Поля лота со страницы: статус, итоговая цена, процедура, объект, координаты, метро, протокол."""
    m = re.search(r"<script[^>]*__NUXT_DATA__[^>]*>(.*?)</script>", html, re.S)
    if not m:
        raise PageError("на странице нет данных лота")
    data = json.loads(m.group(1))

    def res(i, depth=0):   # данные сериализованы ссылками на индексы массива
        v = data[i]
        if depth > 12:
            return None
        if isinstance(v, dict):
            return {k: (res(x, depth + 1) if isinstance(x, int) and x >= 0 else x) for k, x in v.items()}
        if isinstance(v, list):
            if v and v[0] in ("ShallowReactive", "Reactive", "Ref", "ShallowRef"):
                return res(v[1], depth + 1)
            return [res(x, depth + 1) if isinstance(x, int) and x >= 0 else x for x in v]
        return v

    root = res(1)
    payload = next((v for v in (root.get("data") or {}).values() if isinstance(v, dict) and "procedureInfo" in v), None)
    if payload is None:
        raise PageError("в данных страницы нет блока процедуры")
    p = {x["label"]: x["value"] for x in payload.get("procedureInfo") or []}
    o = {x["label"]: x["value"] for x in payload.get("objectInfo") or []}
    head = payload.get("headerInfo") or {}
    coords = (payload.get("mapInfo") or {}).get("coords") or {}
    docs = {}
    for g in (payload.get("documentInfo") or {}).get("documentGroups") or []:
        for f in g.get("files") or []:
            docs.setdefault(f.get("name"), f.get("downloadLink"))
    subway = "; ".join(
        f"{s.get('subwayStationName')} ({s.get('walkingTime')} мин пешком, {s.get('distanceToObject')} км)"
        if isinstance(s, dict) else str(s) for s in head.get("subway") or [])
    fin = _num(p.get("Итоговая цена"))
    return {
        "status": ((payload.get("sidebar") or {}).get("tenderStatusInfo") or {}).get("statusText"),
        "start_price_page": _num(p.get("Начальная цена за объект")),
        "final_price": fin or None,   # 0 на странице — итога ещё нет
        "deposit": _num(p.get("Размер задатка")), "auction_step": _num(p.get("Шаг аукциона")),
        "procedure_form": p.get("Форма проведения"), "price_kind": p.get("Вид начальной цены"),
        "apps_from": p.get("Дата начала приёма заявок"), "apps_to": p.get("Дата окончания приёма заявок"),
        "trades_at": p.get("Проведение торгов"), "results_at": p.get("Подведение итогов"),
        "torgi_gov_url": p.get("Ссылка на torgi.gov.ru"),
        "lat": coords.get("lat"), "lon": coords.get("long"),
        "display_address": head.get("displayAddress"), "subway": subway,
        "house_type": o.get("Тип дома"), "floors_in_house": o.get("Этажность"), "floor": o.get("Этаж"),
        "placement": o.get("Расположение"), "cadastral": o.get("Кадастровый номер"),
        "auction_protocol_url": docs.get("Протокол аукциона") or docs.get("Протокол об итогах аукциона"),
    }


def load(path) -> dict[str, dict]:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open(encoding="utf-8", newline="") as f:
        return {r["tender_id"]: r for r in csv.DictReader(f)}


def save(path, rows: dict[str, dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".csv", dir=p.parent)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows.values(), key=lambda r: (r.get("trades_date") or "", r["tender_id"]), reverse=True):
            w.writerow(r)
    os.replace(tmp, p)   # атомарно: при сбое остаётся прежняя таблица


def candidates(con, state_dir, known: dict[str, dict], today: date, since: date,
               recheck_days: int = 60) -> list[dict]:
    """Лоты машино-мест с прошедшей датой торгов, по которым итога ещё нет в таблице.

    Новые — снятые с публикации лоты с датой торгов от since до вчера. Перепроверяются лоты
    с неокончательным статусом (приём заявок завершён, «состоялись» без цены) не старше recheck_days.
    """
    sql = current_sql(state_dir, 1461)
    if sql is None:
        return []
    rows = con.execute(f"""
        SELECT regexp_extract(json_extract_string(payload, '$.WebSite[0].WebSite'), 'tender/(\\d+)', 1) AS tid,
               try_strptime(json_extract_string(payload, '$.TradesDate'), '%d.%m.%Y')::DATE AS td,
               json_extract_string(payload, '$.AdmArea') AS adm_area,
               json_extract_string(payload, '$.District') AS district,
               json_extract_string(payload, '$.Address') AS address,
               json_extract_string(payload, '$.Coordinates') AS coords,
               try_cast(json_extract_string(payload, '$.Space') AS DOUBLE) AS space,
               try_cast(json_extract_string(payload, '$.StartPrice') AS DOUBLE) AS start_price,
               json_extract_string(payload, '$.TradesForm') AS trades_form
        FROM ({sql})
        WHERE json_extract_string(payload, '$.ObjectType') = 'машино-место'
          AND json_extract_string(payload, '$.Stage') = 'снято с публикации'
    """).fetchall()
    cols = ["tender_id", "trades_date", "adm_area", "district", "address", "coords", "space_m2", "start_price",
            "trades_form"]
    out = []
    for r in rows:
        lot = dict(zip(cols, r))
        tid, td = lot["tender_id"], lot["trades_date"]
        if not tid or td is None or td >= today:
            continue
        old = known.get(tid)
        if old is None:
            if td >= since:
                out.append(lot)
        elif td >= today - timedelta(days=recheck_days):
            status, checked = old.get("status"), (old.get("checked_at") or "")[:10]
            if status not in FINAL + (MISSING,):   # итог ещё не опубликован — смотрим каждый день
                out.append(lot)
            elif status in SOLD and not old.get("final_price") and checked <= (today - timedelta(days=7)).isoformat():
                out.append(lot)   # «состоялись» без цены — цену иногда добавляют позже, раз в неделю
    return sorted(out, key=lambda l: (l["trades_date"], l["tender_id"]), reverse=True)


def to_row(lot: dict, info: dict | None, checked_at: datetime) -> dict:
    space, start = lot.get("space_m2"), lot.get("start_price")
    info = info or {"status": MISSING}
    start = start or info.get("start_price_page")
    fin = info.get("final_price") if info.get("status") in SOLD else None
    lat, lon = info.get("lat"), info.get("lon")
    if not lat and lot.get("coords") and "," in lot["coords"]:
        lat, lon = (float(x) for x in lot["coords"].split(",")[:2])
    row = {k: v for k, v in info.items() if k in FIELDS}
    row.update(
        tender_id=lot["tender_id"], trades_date=str(lot["trades_date"]), adm_area=lot.get("adm_area"),
        district=lot.get("district"), address=lot.get("address"), lat=lat, lon=lon, space_m2=space,
        start_price=start, trades_form=lot.get("trades_form"), final_price=fin,
        start_price_per_m2=round(start / space) if start and space else None,
        final_price_per_m2=round(fin / space) if fin and space else None,
        final_to_start=round(fin / start, 3) if fin and start else None,
        url=LOT_URL.format(lot["tender_id"]), checked_at=f"{checked_at:%Y-%m-%d %H:%M}",
    )
    return row


def fetch_info(tid: str, transport=page_transport, retries: int = 1, sleep=time.sleep) -> dict | None:
    """Поля со страницы лота; None — страницы нет (404). Сетевой сбой повторяется один раз."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            status, html = transport(LOT_URL.format(tid))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        else:
            if status == 404:
                return None
            if status == 200:
                return parse_page(html)
            last = PageError(f"HTTP {status}")
        if attempt < retries:
            sleep(5)
    raise last


def collect(con, state_dir, results_path, today: date, since: date, *, limit: int = 1000, pause: float = 1.0,
            transport=page_transport, sleep=time.sleep, now=None, log=print) -> dict:
    """Дописывает итоги в таблицу. Возвращает счётчики запуска."""
    known = load(results_path)
    todo = candidates(con, state_dir, known, today, since)[:limit]
    stats = {"candidates": len(todo), "new": 0, "rechecked": 0, "sold": 0, "errors": 0}
    checked_at = now or datetime.now()
    for i, lot in enumerate(todo, 1):
        tid = lot["tender_id"]
        try:
            info = fetch_info(tid, transport=transport, sleep=sleep)
        except Exception as e:   # один лот не должен срывать остальные; перепроверим в следующий раз
            stats["errors"] += 1
            log(f"  лот {tid}: {e}")
            continue
        stats["rechecked" if tid in known else "new"] += 1
        row = to_row(lot, info, checked_at)
        if row.get("final_price"):
            stats["sold"] += 1
        known[tid] = row
        if i % 200 == 0:
            save(results_path, known)   # промежуточное сохранение на долгих догрузках
            log(f"  {i} / {len(todo)}")
        if pause:
            sleep(pause)
    save(results_path, known)
    stats["total"] = len(known)
    return stats


OKRUG = {"Центральный": "ЦАО", "Северный": "САО", "Северо-Восточный": "СВАО", "Восточный": "ВАО",
         "Юго-Восточный": "ЮВАО", "Южный": "ЮАО", "Юго-Западный": "ЮЗАО", "Западный": "ЗАО",
         "Северо-Западный": "СЗАО", "Зеленоградский": "ЗелАО", "Новомосковский": "НАО", "Троицкий": "ТАО"}
SLIM = ["tender_id", "trades_date", "okrug", "district", "house", "place", "lat", "lon", "subway", "space_m2",
        "start_price", "status", "final_price", "results_at"]


def export_slim(results_path, out_path, since: date | None = None) -> int:
    """Компактная таблица для дашборда торгов (без длинных адресов и ссылок)."""
    rows = load(results_path).values()
    prefix = re.compile(r"^Российская Федерация, город Москва, внутригородская территория "
                        r"(муниципальный округ|поселение|городской округ) [^,]+, ")

    def i(x):
        v = _num(x)
        return int(v) if v is not None else ""

    def coord(x):
        v = _num(x)
        return round(v, 5) if v is not None else ""

    n = 0
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, SLIM)
        w.writeheader()
        for r in sorted(rows, key=lambda r: (r.get("trades_date") or "", r["tender_id"]), reverse=True):
            if since and (r.get("trades_date") or "") < since.isoformat():
                continue
            subway = re.sub(r";.*$", "", r.get("subway") or "").replace(" (None мин пешком, None км)", "")
            w.writerow({
                "tender_id": r["tender_id"], "trades_date": r.get("trades_date"),
                "okrug": OKRUG.get((r.get("adm_area") or "").replace(" административный округ", ""), r.get("adm_area")),
                "district": re.sub(r"^район ", "", r.get("district") or ""),
                "house": prefix.sub("", r.get("address") or ""),
                "place": re.sub(r"^[^,]*административный округ, ", "", r.get("display_address") or ""),
                "lat": coord(r.get("lat")), "lon": coord(r.get("lon")), "subway": subway.replace("None", "?"),
                "space_m2": r.get("space_m2"), "start_price": i(r.get("start_price")), "status": r.get("status"),
                "final_price": i(r.get("final_price")), "results_at": r.get("results_at"),
            })
            n += 1
    return n
