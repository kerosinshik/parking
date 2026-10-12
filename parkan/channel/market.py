"""Рыночные цены для сравнения с торгами: журнал наблюдений и подбор ориентира для лота.

Пульс выгребает наборы целиком, а рынок так не собрать: открытого набора цен сделок по районам нет.
Поэтому здесь не снимок, а журнал наблюдений — каждая строка «источник X на дату D говорит, что в районе R
сегмента S метрика M равна V». Строки только добавляются (`parkan market-add`), старые не правятся:
так видна история цен, и у каждой цифры в посте есть источник и дата.

Файл — CSV (по умолчанию market/observations.csv в ветке data рядом с итогами торгов): его легко
дописать руками или из сессии Claude, а изменения читаются в git diff.

Ориентир для лота (reference): самое свежее наблюдение не старше max_age_days — сначала по району,
затем по округу, затем по Москве. Уровень попадает в факты, чтобы текст говорил «по округу», а не врал про район.
"""

from __future__ import annotations

import csv
import re
from datetime import date
from pathlib import Path

from . import templates as t

FIELDS = ("observed", "level", "area", "segment", "rooms", "quality", "metric", "value", "source", "url", "n", "note",
          "added")
LEVELS = ("район", "округ", "город")
SEGMENTS = ("вторичка", "новостройки", "все")
ROOMS = ("все", "студия", "1", "2", "3", "4+")
QUALITY = ("все", "низкого качества", "типовые", "улучшенные", "элитные")   # как у Росстата
# метрика → (что это, правдоподобный диапазон ₽): защита от лишнего нуля и перепутанных столбцов
METRICS = {
    "sale_m2": ("цена продажи за м²", 50_000, 3_000_000),
    "rent_m2_month": ("аренда за м² в месяц", 200, 20_000),
}
OKRUGS = {
    "ЦАО": "Центральный", "САО": "Северный", "СВАО": "Северо-Восточный", "ВАО": "Восточный",
    "ЮВАО": "Юго-Восточный", "ЮАО": "Южный", "ЮЗАО": "Юго-Западный", "ЗАО": "Западный",
    "СЗАО": "Северо-Западный", "ЗелАО": "Зеленоградский", "НАО": "Новомосковский", "ТАО": "Троицкий",
}
CITY = "Москва"
DEFAULT_PATH = "market/observations.csv"


def _norm(s: str | None) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"\b(район|поселение|муниципальный округ|административный округ)\b", "", s)
    return re.sub(r"[^a-zа-я0-9]+", "", s)


def okrug_key(s: str | None) -> str | None:
    """«ЦАО», «Центральный административный округ», «Центральный» → «ЦАО»."""
    if not s:
        return None
    for abbr, name in OKRUGS.items():
        if _norm(s) in (_norm(abbr), _norm(name)):
            return abbr
    return None


def area_key(level: str, area: str | None) -> str | None:
    if level == "город":
        return CITY
    if level == "округ":
        return okrug_key(area)
    return _norm(area) or None


def _date(v) -> date | None:
    try:
        return date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------- журнал

def load(path) -> list[dict]:
    """Наблюдения журнала в порядке добавления; нет файла — пустой список (канал работает и без рынка)."""
    if not path or not Path(path).exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        try:
            value = float(r["value"])
        except (TypeError, ValueError):
            continue
        out.append(dict(r, value=value, observed=_date(r.get("observed")), key=area_key(r.get("level"), r.get("area"))))
    return out


def validate(row: dict, today: date) -> list[str]:
    """Проблемы строки для импорта; пустой список — строку можно добавлять."""
    p = []
    obs = _date(row.get("observed"))
    if obs is None:
        p.append("observed — дата ГГГГ-ММ-ДД, на которую источник даёт цифру")
    elif obs > today:
        p.append(f"observed {obs} в будущем")
    level = (row.get("level") or "").strip()
    if level not in LEVELS:
        p.append(f"level — одно из: {', '.join(LEVELS)}")
    elif level == "округ" and not okrug_key(row.get("area")):
        p.append(f"округ «{row.get('area')}» не узнан (ЦАО, САО, … НАО, ТАО)")
    elif level == "район":
        from .html_cards import find_district
        if not find_district(row.get("area")):
            p.append(f"район «{row.get('area')}» не найден среди районов Москвы")
    if (row.get("segment") or "все").strip() not in SEGMENTS:
        p.append(f"segment — одно из: {', '.join(SEGMENTS)}")
    if (row.get("rooms") or "все").strip() not in ROOMS:
        p.append(f"rooms — одно из: {', '.join(ROOMS)}")
    if (row.get("quality") or "все").strip() not in QUALITY:
        p.append(f"quality — одно из: {', '.join(QUALITY)}")
    metric = (row.get("metric") or "").strip()
    if metric not in METRICS:
        p.append(f"metric — одно из: {', '.join(METRICS)}")
    try:
        value = float(str(row.get("value")).replace(" ", "").replace(",", "."))
    except ValueError:
        p.append(f"value «{row.get('value')}» — не число")
    else:
        if metric in METRICS:
            what, lo, hi = METRICS[metric]
            if not lo <= value <= hi:
                p.append(f"value {value:.0f} ₽ неправдоподобно для «{what}» ({lo}–{hi})")
    if not (row.get("source") or "").strip():
        p.append("source — откуда цифра (например, irn.ru)")
    url = (row.get("url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        p.append(f"url «{url}» — нужна ссылка http(s)")
    return p


def _clean(row: dict, today: date) -> dict:
    out = {f: str(row.get(f) or "").strip() for f in FIELDS}
    out["value"] = str(round(float(out["value"].replace(" ", "").replace(",", "."))))
    out["segment"] = out["segment"] or "все"
    out["rooms"] = out["rooms"] or "все"
    out["quality"] = out["quality"] or "все"
    out["observed"] = _date(out["observed"]).isoformat()
    if out["level"] == "округ":
        out["area"] = okrug_key(out["area"])
    elif out["level"] == "город":
        out["area"] = CITY
    out["added"] = out["added"] or today.isoformat()
    return out


def _ident(r: dict) -> tuple:
    return (str(r.get("observed"))[:10], r["level"], area_key(r["level"], r["area"]), r.get("segment") or "все",
            r.get("rooms") or "все", r.get("quality") or "все", r["metric"], r["source"].strip().lower(),
            str(round(float(r["value"]))))


def add(path, new_rows: list[dict], today: date) -> tuple[int, int, list[str]]:
    """Дописывает проверенные строки в журнал. Есть ошибки — не добавляет ничего.

    Возвращает (добавлено, повторов пропущено, ошибки). Повтор — та же цифра того же источника на ту же дату;
    другая цифра на ту же дату — поправка: добавляется, при подборе побеждает последняя.
    """
    errors = []
    for i, r in enumerate(new_rows, 2):   # строка 1 — заголовок
        errors += [f"строка {i}: {e}" for e in validate(r, today)]
    if errors:
        return 0, 0, errors
    path = Path(path)
    have = {_ident(r) for r in _raw(path)}
    fresh, dup = [], 0
    for r in new_rows:
        c = _clean(r, today)
        if _ident(c) in have:
            dup += 1
            continue
        have.add(_ident(c))
        fresh.append(c)
    if fresh:
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists() or path.stat().st_size == 0
        with open(path, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new_file:
                w.writeheader()
            w.writerows(fresh)
    return len(fresh), dup, []


def _raw(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def read_rows(path) -> list[dict]:
    """Строки CSV для импорта: разделитель — запятая или точка с запятой (так сохраняет Excel)."""
    text = Path(path).read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;\t")
    return [{(k or "").strip(): v for k, v in r.items()} for r in csv.DictReader(text.splitlines(), dialect=dialect)]


# ---------------------------------------------------------------- ориентир для лота

def _age(max_age_days, level: str) -> int:
    return max_age_days.get(level, 0) if isinstance(max_age_days, dict) else max_age_days


def reference(obs: list[dict], metric: str, district: str | None, adm: str | None, today: date,
              max_age_days, segment: str = "вторичка", levels=LEVELS) -> dict | None:
    """Самое свежее наблюдение: район → округ → Москва. Сегмент «все» подходит, если нужного нет.

    max_age_days — число или {уровень: дней}: квартальные цифры по Москве живут дольше месячных по районам."""
    targets = [("район", _norm(district) if district else None), ("округ", okrug_key(adm)), ("город", CITY)]
    for seg in (segment, "все"):
        for level, key in targets:
            if not key or level not in levels:
                continue
            hits = [o for o in obs if o["metric"] == metric and o.get("level") == level and o["key"] == key
                    and (o.get("segment") or "все") == seg and (o.get("rooms") or "все") == "все"
                    and (o.get("quality") or "все") == "все"
                    and o["observed"] and 0 <= (today - o["observed"]).days <= _age(max_age_days, level)]
            if hits:
                # свежее — лучше; на одну дату — последняя добавленная (поправка)
                return max(enumerate(hits), key=lambda x: (x[1]["observed"], x[0]))[1]
    return None


def compare(price: float, space: float | None, sale: dict | None, rent: dict | None) -> dict | None:
    """Цифры сравнения с рынком для квартиры; без площади или ориентира продажи — None."""
    if not price or not space or not sale:
        return None
    m2 = price / space
    out = {"price_m2": m2, "market_m2": sale["value"], "gap": 1 - m2 / sale["value"],
           "gain": (sale["value"] - m2) * space, "sale": sale}
    if rent:
        year = rent["value"] * 12 * space
        out.update(rent_month=rent["value"] * space, rent_yield=year / price, payback=price / year, rent=rent)
    return out


def _where(o: dict) -> str:
    if o["level"] == "район":
        return f"по району {t.district(o['area'])}"
    if o["level"] == "округ":
        return f"по {o['area']}"
    return "по Москве"


def _month(d: date) -> str:
    names = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
             "ноябрь", "декабрь")
    return f"{names[d.month - 1]} {d.year}"


def _seg(o: dict) -> str:
    return {"вторичка": "вторичного жилья", "новостройки": "новостроек"}.get(o.get("segment") or "", "жилья")


def _what(o: dict) -> str:
    if o["source"].lower().startswith("росстат"):
        return f"средняя цена квадратного метра {_seg(o)} {_where(o)} по данным Росстата (на конец квартала)"
    return f"средняя цена квадратного метра {_seg(o)} в объявлениях {_where(o)}"


def city_facts(obs: list[dict], today: date, max_age_days) -> dict | None:
    """Фон по Москве (Росстат): средняя цена м² на вторичке, в том числе типовых квартир. Без расчёта выгоды:
    средняя по городу смешивает центр и окраины, и «ниже рынка на 40 %» для квартиры в Некрасовке было бы враньём."""
    sale = reference(obs, "sale_m2", None, None, today, max_age_days, levels=("город",))
    if not sale:
        return None
    typical = [o for o in obs if o["metric"] == "sale_m2" and o["level"] == "город" and o.get("quality") == "типовые"
               and o.get("segment") == sale.get("segment") and o["observed"] == sale["observed"]
               and o["source"] == sale["source"]]
    f = {"moscow_price_per_m2": f"{t.rub(sale['value'])}/м²", "moscow_what": _what(sale),
         "moscow_source": sale["source"], "moscow_period": sale.get("note") or _month(sale["observed"]),
         "moscow_url": sale.get("url") or None,
         "moscow_typical_per_m2": f"{t.rub(typical[-1]['value'])}/м²" if typical else None,
         "moscow_caveat": "средняя по всей Москве: с конкретной квартирой не сравнивать, только как фон"}
    return {k: v for k, v in f.items() if v}


def facts(c: dict | None) -> dict | None:
    """Факты сравнения с рынком для поста: всё уже в формате канала, с источником и оговорками."""
    if not c:
        return None
    s = c["sale"]
    f = {
        "market_price_per_m2": f"{t.rub(c['market_m2'])}/м²",
        "market_what": _what(s),
        "market_source": s["source"], "market_month": _month(s["observed"]), "market_url": s.get("url") or None,
        "lot_price_per_m2": f"{t.rub(c['price_m2'])}/м²",
    }
    if c["gap"] > 0:
        f.update(below_market=f"{t.pct(c['gap'])} %", gross_gain=t.money(c["gain"]),
                 gross_gain_means="(рыночная цена м² − цена м² лота) × площадь: сколько стоила бы квартира по рынку "
                                  "сверх цены покупки, до ремонта, налогов, комиссий и срока продажи")
    else:
        f.update(above_market=f"{t.pct(-c['gap'])} %")
    if c.get("rent"):
        r = c["rent"]
        f.update(rent_per_month=t.money(c["rent_month"]), gross_rent_yield=f"{t.num(c['rent_yield'] * 100)} %",
                 payback_years=f"{t.num(c['payback'], 0)}",
                 rent_what=f"аренда {_where(r)}: {t.rub(r['value'])}/м² в месяц ({r['source']}, {_month(r['observed'])}); "
                           "доходность — до налогов, простоя и расходов на содержание")
    f["caveats"] = ("цены в объявлениях выше цен сделок; средняя по району смешивает дома разного года и состояния; "
                    "это расчёт, а не инвестиционная рекомендация")
    return {k: v for k, v in f.items() if v not in (None, "")}


# ---------------------------------------------------------------- ориентир по итогам торгов (свои данные)

TORGI_DAYS = 90     # окно итогов
TORGI_MIN = 5       # проданных квартир в районе, иначе — округ, иначе ориентира нет


def torgi_sold(results: list[dict], today: date, days: int = TORGI_DAYS) -> list[dict]:
    """Проданные квартиры за окно: tender_id, ключи района и округа, итоговая цена м², дата торгов."""
    out = []
    for r in results or []:
        if r.get("kind") != "flat" or not r.get("final_price"):
            continue
        m2 = r.get("final_price_per_m2") or (r["final_price"] / r["space_m2"] if r.get("space_m2") else None)
        d = _date(r.get("trades_date"))
        if not m2 or not d or not 0 <= (today - d).days <= days:
            continue
        out.append({"id": r.get("tender_id"), "district": _norm(r.get("district")), "okrug": okrug_key(r.get("adm_area")),
                    "area": r.get("district"), "m2": m2, "date": d})
    return out


def torgi_reference(sold: list[dict], district: str | None, adm: str | None, exclude=None,
                    min_n: int = TORGI_MIN) -> dict | None:
    """Медиана итоговой цены м² проданных квартир района (меньше min_n — округа); сам лот в выборку не входит."""
    import statistics
    for level, key, field in (("район", _norm(district) if district else None, "district"), ("округ", okrug_key(adm), "okrug")):
        if not key:
            continue
        hits = [x for x in sold if x[field] == key and (exclude is None or x["id"] != exclude)]
        if len(hits) >= min_n:
            return {"level": level, "area": district if level == "район" else key, "n": len(hits),
                    "value": statistics.median(x["m2"] for x in hits),
                    "since": min(x["date"] for x in hits), "until": max(x["date"] for x in hits)}
    return None


def torgi_facts(ref: dict | None, price, space, days: int = TORGI_DAYS) -> dict | None:
    """Факты: насколько лот дешевле или дороже того, за сколько обычно уходят квартиры района на торгах."""
    if not ref or not price or not space:
        return None
    m2 = price / space
    gap = 1 - m2 / ref["value"]
    where = f"в районе {t.district(ref['area'])}" if ref["level"] == "район" else f"в {ref['area']}"
    f = {"torgi_price_per_m2": f"{t.rub(ref['value'])}/м²",
         "torgi_what": f"медиана итоговой цены квадратного метра квартир, проданных на торгах torgi.mos.ru {where} "
                       f"за последние {days} дней (без этого лота)",
         "torgi_deals": f"{ref['n']} {t.plural(ref['n'], ('квартира', 'квартиры', 'квартир'))}",
         "torgi_period": t.day(ref["since"]) if ref["since"] == ref["until"]
         else f"с {t.day(ref['since'])} по {t.day(ref['until'])}"}
    if gap > 0:
        f.update(below_torgi=f"{t.pct(gap)} %", torgi_difference=t.money(gap * ref["value"] * space),
                 torgi_difference_means="на столько дешевле, чем стоила бы эта площадь по медиане итогов района")
    else:
        f.update(above_torgi=f"{t.pct(-gap)} %", torgi_difference=t.money(-gap * ref["value"] * space),
                 torgi_difference_means="на столько дороже, чем стоила бы эта площадь по медиане итогов района")
    return f


def enrich(rows: list[dict], obs: list[dict], today: date, max_age_days, *, price="price", space="space",
           district="district", adm="adm", sold: list[dict] | None = None, own_id: str | None = None) -> None:
    """Добавляет к строкам-квартирам r['market']: сравнение с рынком района/округа, фон по Москве и сравнение
    с итогами торгов района (sold — torgi_sold(...); own_id — поле с id лота, чтобы не сравнивать лот с собой)."""
    if not obs and not sold:
        return
    local = ("район", "округ")   # сравнение с лотом — только по району или округу, Москва — фоном
    city = city_facts(obs, today, max_age_days) if obs else None
    for r in rows:
        if r.get("kind") != "flat":
            continue
        f = {}
        if obs:
            args = (r.get(district), r.get(adm), today, max_age_days)
            c = compare(r.get(price), r.get(space), reference(obs, "sale_m2", *args, levels=local),
                        reference(obs, "rent_m2_month", *args, levels=local))
            f.update(facts(c) or {}, **(city or {}))
        if sold:
            ref = torgi_reference(sold, r.get(district), r.get(adm), exclude=r.get(own_id) if own_id else None)
            tf = torgi_facts(ref, r.get(price), r.get(space))
            if tf and not own_id:   # лот в продаже: сравниваем его старт с итогами — цена на аукционе ещё может вырасти
                tf["torgi_compare_note"] = ("сравнивается стартовая цена лота с итоговыми ценами проданных квартир; "
                                            "на аукционе цена может вырасти")
            f.update(tf or {})
        if f:
            r["market"] = f


def coverage(obs: list[dict], districts: list[str], today: date, max_age_days: int) -> dict:
    """Для отчёта market-status: по каким районам с квартирами в продаже есть свежий ориентир и какого уровня."""
    out = {"район": [], "округ": [], "город": [], "нет": []}
    for d, adm in districts:
        ref = reference(obs, "sale_m2", d, adm, today, max_age_days)
        out[ref["level"] if ref else "нет"].append(d)
    return out
