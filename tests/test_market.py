from datetime import date

import duckdb
import pytest

from parkan.channel import content, copy, market

from .test_channel import journal, results_csv

TODAY = date(2026, 10, 6)
HEAD = "observed,level,area,segment,rooms,metric,value,source,url,n,note\n"


def obs_csv(tmp_path, body: str, name="new.csv"):
    p = tmp_path / name
    p.write_text(HEAD + body, encoding="utf-8")
    return p


def test_add_validates_and_skips_duplicates(tmp_path):
    log = tmp_path / "market" / "observations.csv"
    bad = obs_csv(tmp_path, "2026-09-30,район,Нет Такого,вторичка,все,sale_m2,500000,irn.ru,,,\n"
                            "2026-09-30,район,Хамовники,вторичка,все,sale_m2,5000000,irn.ru,,,\n"
                            "2026-11-30,округ,ЦАО,вторичка,все,sale_m2,500000,,ftp://x,,\n")
    added, dup, errors = market.add(log, market.read_rows(bad), TODAY)
    assert added == 0 and not log.exists()                       # с ошибками не добавляется ничего
    assert any("Нет Такого" in e for e in errors) and any("неправдоподобно" in e for e in errors)
    assert any("в будущем" in e for e in errors) and any("source" in e for e in errors)

    good = obs_csv(tmp_path, "2026-09-30,район,район Хамовники,вторичка,все,sale_m2,\"520 000\",irn.ru,https://irn.ru/x,,\n"
                             "2026-09-30,округ,Центральный административный округ,,,sale_m2,600000,irn.ru,,,\n")
    assert market.add(log, market.read_rows(good), TODAY) == (2, 0, [])
    assert market.add(log, market.read_rows(good), TODAY) == (0, 2, [])    # повтор не дублируется
    rows = market.load(log)
    assert [(r["area"], r["value"], r["segment"], r["added"]) for r in rows] == [
        ("район Хамовники", 520000, "вторичка", "2026-10-06"), ("ЦАО", 600000, "все", "2026-10-06")]

    semi = tmp_path / "excel.csv"     # Excel сохраняет через точку с запятой
    semi.write_text(HEAD.replace(",", ";") + "2026-10-01;город;Москва;вторичка;все;rent_m2_month;1400;irn.ru;;;\n",
                    encoding="utf-8")
    assert market.add(log, market.read_rows(semi), TODAY) == (1, 0, [])


def test_reference_falls_back_and_respects_age(tmp_path):
    log = tmp_path / "obs.csv"
    market.add(log, market.read_rows(obs_csv(tmp_path,
        "2026-06-01,район,Хамовники,вторичка,все,sale_m2,400000,old,,,\n"
        "2026-09-30,округ,ЦАО,вторичка,все,sale_m2,600000,irn.ru,,,\n"
        "2026-09-30,округ,ЦАО,вторичка,все,sale_m2,610000,irn.ru,,,поправка\n")), TODAY)
    obs = market.load(log)
    ref = market.reference(obs, "sale_m2", "район Хамовники", "Центральный административный округ", TODAY, 60)
    assert ref["level"] == "округ" and ref["value"] == 610000      # районная цифра устарела; поправка побеждает
    assert market.reference(obs, "sale_m2", "район Хамовники", None, TODAY, 60) is None
    assert market.reference(obs, "sale_m2", "район Хамовники", None, TODAY, 200)["source"] == "old"


def test_compare_and_facts():
    sale = {"value": 380_000, "level": "район", "area": "район Бабушкинский", "segment": "вторичка",
            "source": "irn.ru", "observed": date(2026, 9, 30), "url": "https://irn.ru/x"}
    rent = dict(sale, value=1400)
    c = market.compare(17_050_000, 55, sale, rent)
    assert c["gap"] == pytest.approx(0.184, abs=0.001) and c["gain"] == pytest.approx(3_850_000)
    f = market.facts(c)
    assert f["market_price_per_m2"] == "380 000 ₽/м²" and f["below_market"] == "18 %"
    assert f["gross_gain"] == "3,85 млн ₽" and f["market_month"] == "сентябрь 2026"
    assert f["gross_rent_yield"] == "5,4 %" and f["payback_years"] == "18"
    assert "по району Бабушкинский" in f["market_what"] and "не инвестиционная рекомендация" in f["caveats"]
    assert "above_market" in market.facts(market.compare(25_000_000, 55, sale, None))
    assert market.compare(1, None, sale, rent) is None and market.compare(1, 50, None, rent) is None


def test_drafts_with_and_without_market(tmp_path):
    con = duckdb.connect(":memory:")
    journal(con, tmp_path)
    res = results_csv(tmp_path)
    plain = content.drafts(con, tmp_path, TODAY, results_path=res)
    assert not any("market" in (p.facts or {}).get("facts", {}) for p in plain)   # без журнала — как раньше

    log = tmp_path / "obs.csv"
    market.add(log, market.read_rows(obs_csv(tmp_path,
        "2026-09-30,район,Хамовники,вторичка,все,sale_m2,500000,irn.ru,https://irn.ru/x,,\n"
        "2026-09-30,район,Хамовники,вторичка,все,rent_m2_month,2000,irn.ru,,,\n")), TODAY)
    posts = content.drafts(con, tmp_path, TODAY, results_path=res, market_path=log)
    assert [p.key for p in posts] == [p.key for p in plain]          # отбор и тексты не меняются
    assert [p.text for p in posts] == [p.text for p in plain]
    flat_lot = next(p for p in posts if p.rubric == "lot" and p.card["type"] == "flat")
    mk = flat_lot.facts["facts"]["market"]                           # 30 млн ₽ за 100 м² против 500 тыс. ₽/м²
    assert mk["below_market"] == "40 %" and mk["gross_gain"] == "20,00 млн ₽" and mk["gross_rent_yield"] == "8 %"
    parking = next(p for p in posts if p.rubric == "lot" and p.card["type"] == "parking")
    assert "market" not in parking.facts["facts"]                    # машино-места с рынком не сравниваем
    con.close()


def test_copy_check_requires_market_source():
    facts = {"facts": {"market": {"market_price_per_m2": "500 000 ₽/м²", "market_source": "irn.ru"}}}
    assert any("без источника" in p for p in copy.check("Рынок района — 500 000 ₽ за метр.", facts, with_card=False))
    assert copy.check("Рынок района, по данным irn.ru, — 500 000 ₽ за метр.", facts, with_card=False) == []


def test_cli_market_add_and_status(tmp_path, capsys):
    from parkan.cli import main
    con = duckdb.connect(":memory:")
    journal(con, tmp_path / "state")
    con.close()
    log = tmp_path / "obs.csv"
    src = obs_csv(tmp_path, "2026-09-30,район,Хамовники,вторичка,все,sale_m2,500000,irn.ru,,,\n")
    assert main(["market-add", "--file", str(src), "--market", str(log), "--today", "2026-10-06"]) == 0
    assert main(["market-status", "--market", str(log), "--state-dir", str(tmp_path / "state"),
                 "--today", "2026-10-06"]) == 0
    out = capsys.readouterr().out
    assert "Добавлено наблюдений: 1" in out and "ориентир по району — 1" in out and "нет — 0" in out
    bad = obs_csv(tmp_path, "2026-09-30,район,Хамовники,вторичка,все,sale_m2,abc,irn.ru,,,\n", "bad.csv")
    assert main(["market-add", "--file", str(bad), "--market", str(log), "--today", "2026-10-06"]) == 1


def test_market_page(tmp_path):
    import json
    import re
    from parkan.cli import main
    con = duckdb.connect(":memory:")
    journal(con, tmp_path / "state")
    con.close()
    out = tmp_path / "market.html"
    args = ["market-page", "--state-dir", str(tmp_path / "state"), "--market", str(tmp_path / "none.csv"),
            "--out", str(out), "--today", "2026-10-06"]
    assert main(args) == 0                                           # без журнала рынка страница строится
    data = json.loads(re.search(r"const D = (.*?);\n", out.read_text(encoding="utf-8")).group(1))
    assert data["journal"] == [] and len(data["flats"]) == 12 and data["coverage"]["нет"] == 1
    log = tmp_path / "obs.csv"
    market.add(log, market.read_rows(obs_csv(tmp_path,
        "2026-09-30,район,Хамовники,вторичка,все,sale_m2,500000,irn.ru,,,\n"
        "2026-09-30,округ,ЦАО,вторичка,все,sale_m2,550000,irn.ru,,,\n")), TODAY)
    assert main(args[:4] + [str(log)] + args[5:]) == 0
    data = json.loads(re.search(r"const D = (.*?);\n", out.read_text(encoding="utf-8")).group(1))
    assert data["coverage"] == {"район": 1, "округ": 0, "город": 0, "нет": 0}
    cheap = next(f for f in data["flats"] if f["place"] == "ул. Квартирная, 2")
    assert cheap["gap"] == pytest.approx(0.4) and data["okrugs"][0]["market"]["value"] == 550000


def _xlsx(rows: list[list]) -> bytes:
    """Минимальный XLSX: лист «Содержание» и лист с данными (строки инлайн), как у Росстата."""
    import io
    import zipfile

    def cell(r, c, v):
        ref = chr(65 + c) + str(r)
        if isinstance(v, (int, float)):
            return f'<c r="{ref}"><v>{v}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>'

    def sheet(data):
        body = "".join(f'<row r="{i}">' + "".join(cell(i, j, v) for j, v in enumerate(r) if v is not None) + "</row>"
                       for i, r in enumerate(data, 1))
        return ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                + body + "</sheetData></worksheet>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet([["Содержание:"]]))
        z.writestr("xl/worksheets/sheet2.xml", sheet(rows))
    return buf.getvalue()


ROSSTAT_ROWS = [
    ["К содержанию", "Средние цены на вторичном рынке жилья"],
    [None, "I квартал", None, None, None, None, "II квартал"],
    [None, "Все типы квартир", "низкого качества", "квартиры среднего качества (типовые)", "улучшенного качества",
     "элитные квартиры", "Все типы квартир", "низкого качества", "квартиры среднего качества (типовые)",
     "улучшенного качества", "элитные квартиры"],
    ["643 - Российская Федерация", 143953.4, 89573.37, 118114.82, 163949.83, 293868.03, 146469.57, 1, 2, 3, 4],
    ["45000000000 - Город Москва столица Российской Федерации город федерального значения",
     381296.64, "-", 311211.84, 369552.98, 562891.71, 391066.76, "-", 333154.95, 372122.56, 587926.32],
]


def test_rosstat_parse_and_city_background(tmp_path):
    from parkan.channel import rosstat
    page = ('<a href="/storage/mediabank/sred_cen_perv_2kv-2026.xlsx">x</a>'
            '<a href="/storage/mediabank/sred_cen_vtor_2kv-2026.xlsx">x</a>'
            '<a href="/storage/mediabank/sred_cen_vtor_4kv-2025.xlsx">x</a>')
    assert rosstat.latest_files(page)["vtor"].endswith("sred_cen_vtor_2kv-2026.xlsx")
    rows = rosstat.parse(_xlsx(ROSSTAT_ROWS), "вторичка", 2026, "https://rosstat.gov.ru/x.xlsx")
    assert [(r["observed"], r["quality"], r["value"]) for r in rows] == [
        ("2026-03-31", "все", "381297"), ("2026-03-31", "типовые", "311212"), ("2026-03-31", "улучшенные", "369553"),
        ("2026-03-31", "элитные", "562892"), ("2026-06-30", "все", "391067"), ("2026-06-30", "типовые", "333155"),
        ("2026-06-30", "улучшенные", "372123"), ("2026-06-30", "элитные", "587926")]
    log = tmp_path / "obs.csv"
    assert market.add(log, rows, TODAY) == (8, 0, [])
    obs = market.load(log)
    age = content.POLICY["market_max_age_days"]
    # с лотом Москву не сравниваем: только фон, без «ниже рынка» и выгоды
    r = {"kind": "flat", "price": 10_000_000, "space": 50, "district": "район Некрасовка",
         "adm": "Юго-Восточный административный округ"}
    market.enrich([r], obs, TODAY, age)
    assert r["market"]["moscow_price_per_m2"] == "391 067 ₽/м²" and r["market"]["moscow_typical_per_m2"] == "333 155 ₽/м²"
    assert "below_market" not in r["market"] and "gross_gain" not in r["market"]
    assert "по данным Росстата" in r["market"]["moscow_what"] and r["market"]["moscow_period"] == "II квартал 2026, на конец квартала"
    stale = {k: v for k, v in r.items() if k != "market"}
    market.enrich([stale], obs, date(2027, 3, 1), age)                # квартал устарел — фона нет
    assert "market" not in stale
    f = {"facts": {"market": r["market"]}}
    assert any("по Москве без источника" in p for p in copy.check("В Москве — 391 067 ₽ за метр.", f, with_card=False))
    assert copy.check("По данным Росстата, в Москве — 391 067 ₽ за метр.", f, with_card=False) == []


def test_torgi_reference_excludes_self_and_falls_back():
    def row(i, district, m2, d="2026-09-01", adm="Центральный административный округ"):
        return {"kind": "flat", "tender_id": str(i), "final_price": m2 * 50, "space_m2": 50, "district": district,
                "adm_area": adm, "trades_date": d}
    results = [row(i, "район Хамовники", 400_000 + i * 10_000) for i in range(6)]
    results += [row(10, "Басманный район", 300_000), row(11, "район Хамовники", 999_999, d="2026-05-01"),   # вне окна
                {"kind": "parking", "tender_id": "12", "final_price": 1, "space_m2": 13, "trades_date": "2026-09-01"}]
    sold = market.torgi_sold(results, TODAY)
    assert len(sold) == 7
    ref = market.torgi_reference(sold, "район Хамовники", "Центральный административный округ")
    assert ref["level"] == "район" and ref["n"] == 6 and ref["value"] == 425_000
    ref = market.torgi_reference(sold, "район Хамовники", "ЦАО", exclude="0")   # без самого лота — 5, ещё район
    assert ref["level"] == "район" and ref["n"] == 5
    ref = market.torgi_reference(sold, "Басманный район", "Центральный административный округ")
    assert ref["level"] == "округ" and ref["n"] == 7                           # в Басманном одна продажа — округ
    f = market.torgi_facts(market.torgi_reference(sold, "район Хамовники", "ЦАО"), 17_000_000, 50)
    assert f["below_torgi"] == "20 %" and f["torgi_difference"] == "4,25 млн ₽" and f["torgi_deals"] == "6 квартир"
    assert "районе Хамовники" in f["torgi_what"] and f["torgi_period"] == "1 сентября"
    assert "above_torgi" in market.torgi_facts(market.torgi_reference(sold, "район Хамовники", "ЦАО"), 25_000_000, 50)
    r = {"kind": "flat", "price": 17_000_000, "space": 50, "district": "район Хамовники", "adm": "ЦАО"}
    market.enrich([r], [], TODAY, content.POLICY["market_max_age_days"], sold=sold)   # без журнала рынка тоже работает
    assert r["market"]["below_torgi"] == "20 %"
