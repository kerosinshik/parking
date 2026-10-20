"""Аналитика года для статьи: расчёт по таблице итогов (без браузера)."""

import csv

from parkan.channel import yearstats

COLS = ("trades_date", "adm_area", "display_address", "subway", "cadastral", "start_price", "final_price",
        "final_to_start", "status", "trades_form")
SOLD = "Признаны состоявшимися"
AO = "Северный административный округ"
AUCTION = "открытый аукцион в электронной форме"


def lot(date, house, place, price, final=None, status=SOLD, walk=5):
    return {"trades_date": date, "adm_area": AO, "subway": f"Речной вокзал ({walk} мин пешком, 0.4 км)",
            "display_address": f"{AO}, {house}, Подземный этаж № -1, м/м {place}", "cadastral": f"77:09:1:{house}{place}",
            "start_price": price, "final_price": final if status == SOLD else "",
            "final_to_start": (final / price) if status == SOLD and final else "", "status": status, "trades_form": AUCTION}


def write(tmp_path, rows):
    p = tmp_path / "torgi.csv"
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, COLS)
        w.writeheader()
        w.writerows(rows)
    return p


def test_trend_in_house_removes_house_mix(tmp_path):
    # горячий дом продаётся зимой (всегда с торгом), тихий — летом (всегда по старту);
    # в каждом доме есть одна продажа и в «чужом» сезоне, иначе поправку не посчитать
    rows = [lot("2025-11-10", "Фестивальная ул., д. 15", i, 1_000_000, 2_000_000) for i in range(10)]
    rows += [lot("2026-07-10", "Фестивальная ул., д. 15", 99, 1_000_000, 2_000_000)]
    rows += [lot("2026-07-10", "Лавочкина ул., д. 6", i, 2_000_000, 2_000_000) for i in range(10)]
    rows += [lot("2025-11-10", "Лавочкина ул., д. 6", 99, 2_000_000, 2_000_000)]
    f = yearstats.compute(write(tmp_path, rows), min_house_sales=5)
    t = {r["month"]: r for r in f["trend"]}
    assert round(t["2025-11"]["raw"], 2) == 0.09 and round(t["2026-07"]["raw"], 2) == 0.91
    assert abs(t["2025-11"]["in_house"] - t["2026-07"]["in_house"]) < 0.01   # внутри домов тренда нет
    assert f["houses"]["buckets"] == [1, 0, 0, 0, 1]
    assert f["totals"]["sold"] == 22 and f["totals"]["at_start"] == 11


def test_relist_after_nosale_and_funnel(tmp_path):
    rows = [lot("2026-03-01", "Базовская ул., д. 26", 1, 2_000_000, status="Признаны несостоявшимися"),
            lot("2026-06-01", "Базовская ул., д. 26", 1, 2_000_000, 2_000_000),
            lot("2026-03-01", "Базовская ул., д. 26", 2, 2_000_000, status="Признаны несостоявшимися"),
            lot("2026-06-01", "Базовская ул., д. 26", 2, 2_200_000, status="Признаны несостоявшимися"),
            lot("2026-03-01", "Базовская ул., д. 26", 3, 2_000_000, 2_400_000)]
    f = yearstats.compute(write(tmp_path, rows))
    assert f["funnel"] == {"places": 3, "first_try": 1, "later": 1, "pending": 0, "unsold": 1}
    rl = f["relist"]
    assert rl["relisted"] == 2 and rl["same"] == 0.5 and rl["dearer"] == 0.5 and rl["cheaper"] == 0
    assert rl["sold_next"] == 0.5 and rl["days"] == 92


def test_format():
    assert yearstats.num(15496) == "15 496"
    assert yearstats.pct(0.6) == "60 %"
    assert yearstats.dec(15.19) == "15,2"
    assert yearstats.month_label("2026-01") == "янв 26" and yearstats.month_label("2026-03") == "мар"
