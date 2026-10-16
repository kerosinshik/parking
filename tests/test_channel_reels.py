"""Кадры роликов для Instagram Reels: числа и тексты сценария (без браузера)."""

import json

import pytest

from parkan.channel import reels

FACTS = {"type": "parking", "price": 1_290_000, "median": 2_300_000,
         "address": "ул. Примерная, 1", "district": "Кузьминки", "area": 14.1,
         "scope": "округа", "until": "заявки до 3 ноября"}


def test_money_and_discount():
    assert reels.money(1_290_000) == ("1,29", "млн ₽")
    assert reels.money(2_300_000) == ("2,3", "млн ₽")
    assert reels.money(12_500_000) == ("12,5", "млн ₽")
    assert reels.money(850_000) == ("850", "тыс. ₽")
    assert reels.discount(1_290_000, 2_300_000) == 44


def test_frames_follow_script():
    fr = reels.frames(FACTS)
    assert [f["name"] for f in fr] == ["01-price", "02-place", "03-compare", "04-caveat", "05-cta"]
    assert "1,29" in fr[0]["html"] and "машино-место" in fr[0]["html"]
    assert "ул. Примерная, 1" in fr[1]["html"] and "ЮВАО" in fr[1]["html"] and "14,1 м²" in fr[1]["html"]
    assert "заявки до 3 ноября" in fr[1]["html"]
    assert "−44%" in fr[2]["html"] and "2,3 млн ₽" in fr[2]["html"] and "Медиана округа" in fr[2]["html"]
    assert "стартовая" in fr[3]["html"]
    assert "Попутный метр" in fr[4]["html"]


def test_load_facts_requires_numbers(tmp_path):
    p = tmp_path / "f.json"
    p.write_text(json.dumps({"price": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="median"):
        reels.load_facts(p)


def test_campaign_word_and_register(tmp_path):
    fr = reels.frames({**FACTS, "word": "можайка"})
    assert "МОЖАЙКА" in fr[4]["html"]
    p = tmp_path / "campaigns.json"
    reels.register({**FACTS, "campaign": "igr01", "lot_id": "42", "word": "можайка"}, p)
    assert json.loads(p.read_text(encoding="utf-8"))["igr01"] == {"lot": "42", "word": "можайка", "title": "ул. Примерная, 1"}
    with pytest.raises(ValueError, match="занято"):
        reels.register({**FACTS, "campaign": "igr02", "word": "Можайка"}, p)
    assert reels.bot_link("igr01").endswith("?start=igr01")


TOP = {"format": "top", "type": "parking",
       "lots": [{"price": 504_000, "address": "16-я Парковая улица, дом 20А", "district": "район Северное Измайлово",
                 "until": "заявки до 3 ноября", "lot_id": "1"},
                {"price": 510_840, "address": "Щёлковское шоссе, дом 61", "district": "район Гольяново"}],
       "adm": [{"name": "Юго-Западный административный округ", "med": 1_570_000},
               {"name": "Западный административный округ", "med": 2_790_000}]}


def test_top_frames():
    fr = reels.frames(TOP)
    assert [f["name"] for f in fr] == ["01-cheapest", "02-top", "03-adm", "04-caveat", "05-cta"]
    assert "504" in fr[0]["html"] and "Самое дешёвое" in fr[0]["html"]
    assert "ТОП-2" in fr[1]["html"] and "511 тыс. ₽" in fr[1]["html"] and "ВАО · заявки до 3 ноября" in fr[1]["html"]
    assert "ЮЗАО" in fr[2]["html"] and "ЗАО" in fr[2]["html"] and "×1,8" in fr[2]["html"]
    assert "стартовые" in fr[3]["html"] and "Лот № 1" in fr[4]["html"]
    assert reels.adm_short("Новомосковский административный округ") == "НАО"


def _con(rows):
    import duckdb
    con = duckdb.connect()
    con.execute("CREATE TEMP TABLE ch_sale (lot_id VARCHAR, kind VARCHAR, address VARCHAR, district VARCHAR, adm VARCHAR, "
                "space DOUBLE, price DOUBLE, end_d DATE, trades_d DATE, site VARCHAR)")
    con.executemany("INSERT INTO ch_sale VALUES (?, 'parking', ?, ?, ?, 12, ?, ?, NULL, NULL)", rows)
    return con


def test_top_facts_skip_closing_lots_and_small_okrugs():
    from datetime import date
    today = date(2026, 10, 16)
    rows = [("1", "Щербинка, 5", "район Щербинка", "НАО", 356_000, date(2026, 10, 19)),   # заявки скоро закончатся
            ("2", "Парковая, 20", "район Северное Измайлово", "ВАО", 504_000, date(2026, 11, 3)),
            ("3", "Парковая, 20", "район Северное Измайлово", "ВАО", 520_000, date(2026, 11, 3)),  # тот же дом
            ("4", "Щёлковское, 61", "район Гольяново", "ВАО", 510_840, date(2026, 11, 26))]
    rows += [(f"w{i}", f"Западная, {i}", "район Кунцево", "ЗАО", 2_000_000 + i, date(2026, 11, 20)) for i in range(3)]
    f = reels.top_facts(_con(rows), today, n=3, min_houses=2)
    assert [l["lot_id"] for l in f["lots"]] == ["2", "4", "w0"]
    assert f["lot_id"] == "2" and f["lots"][0]["until"] == "заявки до 3 ноября"
    assert [a["short"] for a in f["adm"]] == ["ВАО", "ЗАО"]   # НАО — один дом, не берётся
