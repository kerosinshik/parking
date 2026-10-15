"""Факты поста для автора текста (Claude по навыку .claude/skills/channel-copy).

Только то, что есть в данных, уже в формате канала: деньги, даты, проценты, адрес без «внутригородской
территории». Пишущий текст берёт цифры отсюда и ничего не досчитывает сам. Проверка — copy.check.
"""

from __future__ import annotations

from datetime import timedelta

from . import templates as t

TITLES = {"lot": "Лот дня", "drop": "Снижение цены", "house": "Целый дом", "sold": "Продано дороже",
          "nofight": "Без борьбы", "week": "Две недели вперёд", "weekstats": "Итоги недели",
          "deadline": "Заявки заканчиваются", "snapshot": "Срез торгов",
          "torgi": "Старт ниже итогов", "market": "Рядом на рынке"}
TAGS = {"lot": "#лотдня", "drop": "#снижение", "house": "#целыйдом", "sold": "#итоги", "nofight": "#безборьбы",
        "week": "#двенеделивперёд", "weekstats": "#итогинедели",
        "deadline": "#заявкизаканчиваются", "snapshot": "#срезторгов", "torgi": "#стартнижеитогов", "market": "#рядомнарынке"}


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _base(rubric: str, kind: str, **fields) -> dict:
    w = t.WORDS[kind]
    return {"rubric": rubric, "rubric_title": TITLES[rubric], "object": w["forms"][0],
            "facts": _clean(fields)}


def _per_m2(price, space) -> str | None:
    return f"{t.rub(price / space)}/м²" if price and space else None


def lot(r: dict) -> dict:
    return _base("lot", r["kind"], address=r["place"], district=t.district(r.get("district")),
                 price=t.money(r["price"]), price_per_m2=_per_m2(r["price"], r.get("space")), area=t.area(r.get("space")),
                 discount=f"{t.pct(r['discount'])} %",
                 compared_with=("цена за м² с медианой цены за м² " if r["kind"] == "flat" else "цена лота с медианой цен ")
                 + f"{'округа' if r.get('scope') == 'округа' else 'района'} среди лотов того же типа в продаже: "
                 "сначала медиана каждого дома, потом медиана по другим домам (свой дом не считается)",
                 median_houses=f"{r['ref_houses']} {t.plural(r['ref_houses'], ('дом', 'дома', 'домов'))}"
                 if r.get("ref_houses") else None,
                 median=t._ref_text(r), lots_at_address=r.get("at_addr") if (r.get("at_addr") or 1) > 1 else None,
                 applications_until=t.day(r.get("end_d")), trades_date=t.day(r.get("trades_d")), lot_url=r.get("url"),
                 market=r.get("market"))


def drop(r: dict) -> dict:
    return _base("drop", r["kind"], address=r["place"], district=t.district(r.get("district")),
                 old_price=t.money(r["old_price"]), new_price=t.money(r["price"]), drop=f"{t.pct(r['drop'])} %",
                 area=t.area(r.get("space")), price_per_m2=_per_m2(r["price"], r.get("space")),
                 applications_until=t.day(r.get("end_d")), lot_url=r.get("url"), market=r.get("market"))


def torgi(r: dict) -> dict:
    """«Старт ниже итогов»: квартира в продаже и итоги торгов в её доме / районе (поле market, market.torgi_facts)."""
    return _base("torgi", r["kind"], address=r["place"], district=t.district(r.get("district")),
                 start_price=t.money(r["price"]), start_price_per_m2=_per_m2(r["price"], r.get("space")),
                 area=t.area(r.get("space")), applications_until=t.day(r.get("end_d")),
                 trades_date=t.day(r.get("trades_d")), lot_url=r.get("url"), market=r.get("market"))


def house(r: dict) -> dict:
    w = t.WORDS[r["kind"]]
    return _base("house", r["kind"], address=r["place"], district=t.district(r.get("district")),
                 lots_in_house=f"{r['n']} {t.plural(r['n'], w['forms'])}", price_from=t.money(r["price_min"]),
                 price_to=t.money(r["price_max"]) if r["price_max"] > r["price_min"] else None,
                 area=t.area_range(r.get("space_min"), r.get("space_max")),
                 median=t._ref_text(r) if r.get("ref") else None,
                 applications_until=t.day(r.get("end_d")), cheapest_lot_url=r.get("url"))


def _result(rubric: str, r: dict, **extra) -> dict:
    sub = templates_subway(r.get("subway"))
    return _base(rubric, r["kind"], address=r["place"], district=t.district(r.get("district")), metro=sub,
                 start_price=t.money(r["start_price"]) if r.get("start_price") else None,
                 final_price=t.money(r["final_price"]), area=t.area(r.get("space_m2")),
                 final_price_per_m2=_per_m2(r["final_price"], r.get("space_m2")),
                 trades_date=t.day(r.get("trades_date")), status=r.get("status"),
                 lot_url=r.get("url"), protocol_url=r.get("auction_protocol_url"), market=r.get("market"),
                 **extra)


def templates_subway(s: str | None) -> str | None:
    line = t._subway(s)
    return line.removeprefix("🚇 ") if line else None


def sold(r: dict) -> dict:
    return _result("sold", r, growth=f"в {t.num(r['final_to_start'])} раза",
                   overpay=t.money(r["final_price"] - r["start_price"]) if r.get("start_price") else None)


def nofight(r: dict, same: int) -> dict:
    w = t.WORDS[r["kind"]]
    return _result("nofight", r, participants="1", sold_at_start_price="да",
                   same_house_same_day=f"ещё {same} {t.plural(same, w['forms'])}" if same else None)


def _ahead(lots: list[dict]) -> list[dict]:
    return [_clean({"discount": f"{t.pct(l['discount'])} %", "price": t.money(l["price"]), "area": t.area(l.get("space")),
                    "address": l["place"], "district": t.district(l.get("district")),
                    "applications_until": t.day(l.get("end_d")), "lot_url": l.get("url")}) for l in lots]


def week(kind: str, lots: list[dict], today, days: int = 14) -> dict:
    return _base("week", kind, period=f"заявки закрываются до {t.day(today + timedelta(days=days))}",
                 compared_with="цена за м² с медианой района" if kind == "flat" else "цена лота с медианой района",
                 lots=_ahead(lots))


def deadline(kind: str, lots: list[dict], until) -> dict:
    return _base("deadline", kind, applications_until=t.day(until),
                 compared_with="цена за м² с медианой района" if kind == "flat" else "цена лота с медианой района",
                 lots=_ahead(lots))


def snapshot(kind: str, st: dict) -> dict:
    med = (lambda v: t.money(v)) if kind == "parking" else (lambda v: f"{t.rub(v)}/м²")
    return _base("snapshot", kind, date=t.day(st["today"]), on_sale=str(st["n"]), houses=str(st["houses"]),
                 median_start=med(st["med"]),
                 median_explained="медиана стартовой цены " + ("за м² " if kind == "flat" else "")
                                  + "всех лотов этого типа в продаже на торгах города (не рыночная цена)",
                 cheaper_than_district=str(st["cheap"]), cheaper_threshold="25 %",
                 cheaper_explained="лоты, у которых старт ниже медианы лотов того же типа в своём районе на 25 % и больше",
                 cheapest_okrug=_clean({"okrug": t.okrug(st["adm_low"]["adm"]), "median": med(st["adm_low"]["med"]),
                                        "lots": str(st["adm_low"]["n"])}) if st.get("adm_low") else None,
                 priciest_okrug=_clean({"okrug": t.okrug(st["adm_high"]["adm"]), "median": med(st["adm_high"]["med"]),
                                        "lots": str(st["adm_high"]["n"])}) if st.get("adm_high") else None,
                 results_period=f"последние {st['days']} дней" if st.get("sold") else None,
                 sold=str(st["sold"]) if st.get("sold") else None,
                 sold_at_start_share=f"{t.pct(st['at_start'] / st['sold'])} %" if st.get("sold") else None,
                 median_growth=f"×{t.num(st['growth'], 2)}" if st.get("growth") else None)


def weekstats(kind: str, st: dict) -> dict:
    rec = st.get("record")
    return _base("weekstats", kind, period=st["period"], auctions=str(st["total"]), sold=str(st["sold"]),
                 sold_at_start_price=str(st["at_start"]),
                 sold_at_start_share=f"{t.pct(st['at_start'] / st['sold'])} %" if st["sold"] else None,
                 not_held=str(st["failed"]), cancelled_by_city=str(st["cancelled"]),
                 statuses_explained="not_held — торги признаны несостоявшимися: до аукциона не дошло, обычно потому, "
                                    "что не было заявок; лот не продан и может вернуться на торги. cancelled_by_city — "
                                    "город сам отменил торги до их проведения (лот сняли).",
                 record=_clean({"growth": f"×{t.num(rec['final_to_start'])}", "address": rec["place"],
                                "district": t.district(rec.get("district")), "start_price": t.money(rec["start_price"]),
                                "final_price": t.money(rec["final_price"]), "lot_url": rec.get("url")}) if rec else None)
