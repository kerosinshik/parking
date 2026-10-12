import json
from datetime import date, datetime

import duckdb
import pytest

from parkan.channel import content, html_cards, lots, telegram, templates
from parkan.track import digest, torgi
from parkan.track.sync import sync_dataset

from .test_track import LOTS, FakeClient


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    yield c
    c.close()


def lot(i, price, addr, start="01.10.2026", end="30.10.2026", kind="машино-место", space=13.3,
        district="район Пресненский", moscow="да"):
    return {"global_id": 7000 + i, "ID": i, "ObjectType": kind, "Stage": "опубликовано", "OnTerritoryOfMoscow": moscow,
            "StartPrice": price, "Space": space, "District": district if moscow == "да" else None,
            "AdmArea": "Центральный административный округ" if moscow == "да" else None, "Address": addr,
            "StartReceptionDate": start, "EndReceptionDate": end, "TradesDate": "05.11.2026",
            "WebSite": [{"WebSite": f"https://torgi.mos.ru/tender/{i}"}]}


def flat(i, price, addr, space=50, **kw):
    return lot(i, price, addr, kind="квартира", space=space, district="район Хамовники", **kw)


def journal(con, tmp_path):
    """Два дня журнала. На второй: машино-место на 40 % дешевле района и снижение цены у старого;
    квартира на 40 % дешевле за м² (при большей цене лота), пять новых квартир в одном доме,
    дешёвая квартира в Подмосковье (не должна попасть никуда)."""
    rows = [lot(i, 2_000_000 + i * 1000, f"ул. {i}") for i in range(8)]
    rows += [flat(200 + i, 25_000_000, f"ул. Старая, {i}") for i in range(6)]
    sync_dataset(con, FakeClient({1461: rows}), LOTS, tmp_path, observed_at=datetime(2026, 10, 5, 7))
    rows.append(lot(100, 1_200_000, "ул. Дешёвая, 1", start="05.10.2026", end="09.10.2026"))
    rows[0]["StartPrice"] = 1_500_000
    rows.append(flat(300, 30_000_000, "ул. Квартирная, 2", space=100, start="05.10.2026"))
    rows += [flat(400 + i, 26_000_000 + i * 100_000, "ул. Новая, 3", start="05.10.2026") for i in range(5)]
    rows.append(flat(500, 3_000_000, "Московская обл., г. Подольск", moscow="нет", start="05.10.2026"))
    sync_dataset(con, FakeClient({1461: rows}), LOTS, tmp_path, observed_at=datetime(2026, 10, 6, 7))


ADDR = "Российская Федерация, город Москва, внутригородская территория муниципальный округ Хамовники, "


def results_csv(tmp_path):
    rows = {
        "1": {"tender_id": "1", "trades_date": "2026-10-02", "district": "район Хамовники", "adm_area": "ЦАО",
              "address": ADDR + "улица Льва Толстого, дом 5", "space_m2": "14.1", "start_price": "1000000",
              "final_price": "2900000", "final_to_start": "2.9", "final_price_per_m2": "205674",
              "status": "Признаны состоявшимися", "subway": "Парк культуры (7 мин пешком, 0.5 км)",
              "url": "https://torgi.mos.ru/tender/1", "checked_at": "2026-10-05 07:00"},
        "2": {"tender_id": "2", "trades_date": "2026-10-02", "start_price": "1000000", "final_price": "1000000",
              "final_to_start": "1.0", "status": "Признаны состоявшимися", "checked_at": "2026-10-05 07:00"},
        "3": {"tender_id": "3", "trades_date": "2026-10-02", "status": "Признаны несостоявшимися",
              "checked_at": "2026-10-05 07:00"},
        "4": {"tender_id": "4", "trades_date": "2026-08-02", "start_price": "1000000", "final_price": "3000000",
              "final_to_start": "3.0", "status": "Признаны состоявшимися", "checked_at": "2026-08-05 07:00"},
        "11": {"tender_id": "11", "object_type": "квартира", "trades_date": "2026-10-03", "district": "район Хамовники",
               "adm_area": "Центральный административный округ", "address": ADDR + "Пречистенка, дом 1",
               "space_m2": "40", "start_price": "10000000", "final_price": "13000000", "final_to_start": "1.3",
               "status": "Признаны состоявшимися", "checked_at": "2026-10-05 07:00"},
        "12": {"tender_id": "12", "object_type": "квартира", "trades_date": "2026-10-03", "district": "район Хамовники",
               "adm_area": "Центральный административный округ", "address": ADDR + "Остоженка, дом 2",
               "space_m2": "50", "start_price": "15000000", "final_price": "15000000", "final_to_start": "1.0",
               "status": "Единственный участник", "checked_at": "2026-10-05 07:00"},
        "13": {"tender_id": "13", "object_type": "квартира", "trades_date": "2026-10-03", "district": "район Хамовники",
               "adm_area": "Центральный административный округ", "address": ADDR + "Остоженка, дом 2",
               "space_m2": "45", "start_price": "14000000", "final_price": "14000000", "final_to_start": "1.0",
               "status": "Единственный участник", "checked_at": "2026-10-05 07:00"},
        "14": {"tender_id": "14", "object_type": "квартира", "trades_date": "2026-10-03", "address": "Московская обл.",
               "start_price": "1000000", "final_price": "5000000", "final_to_start": "5.0",
               "status": "Признаны состоявшимися", "checked_at": "2026-10-05 07:00"},
    }
    p = tmp_path / "torgi.csv"
    torgi.save(p, rows)
    return p


def test_finding_carries_numbers(con, tmp_path):
    journal(con, tmp_path)
    found = digest.lot_findings(con, tmp_path, date(2026, 10, 6), digest.previous_sync(tmp_path, [1461]))
    cheap = next(f for f in found if f.kind == "дешевле района")
    assert cheap.data["price"] == 1_200_000 and cheap.data["discount"] == pytest.approx(0.4, abs=0.01)
    assert cheap.data["until"] == "09.10.2026" and cheap.data["url"].endswith("/tender/100")
    drop = next(f for f in found if f.kind == "снижение цены")
    assert (drop.data["old_price"], drop.data["price"]) == (2_000_000, 1_500_000)


def test_lots_compare_parking_by_price_and_flats_by_m2(con, tmp_path):
    journal(con, tmp_path)
    assert lots.load(con, tmp_path, date(2026, 10, 6))
    deals = {r["kind"]: r for r in lots.new_deals(con, tmp_path, date(2026, 10, 6), 0.25)}
    assert set(deals) == {"parking", "flat"}                       # Подмосковье не попало
    assert deals["parking"]["discount"] == pytest.approx(0.4, abs=0.01)
    assert deals["flat"]["place"] == "ул. Квартирная, 2" and deals["flat"]["discount"] == pytest.approx(0.4, abs=0.01)
    houses = lots.new_houses(con, tmp_path, date(2026, 10, 6), {"parking": 10, "flat": 5})
    assert [(h["kind"], h["place"], h["n"]) for h in houses] == [("flat", "ул. Новая, 3", 5)]
    drops = lots.price_drops(con, tmp_path, date(2026, 10, 6), 0.1)
    assert [(d["kind"], d["old_price"], d["price"]) for d in drops] == [("parking", 2_000_000, 1_500_000)]
    assert {r["tender_id"] for r in lots.results(results_csv(tmp_path))} == {"1", "2", "3", "4", "11", "12", "13"}


def test_drafts_all_rubrics_and_registry(con, tmp_path):
    journal(con, tmp_path)
    res = results_csv(tmp_path)
    tuesday = date(2026, 10, 6)
    posts = content.drafts(con, tmp_path, tuesday, results_path=res)
    by = {}
    for p in posts:
        by.setdefault(p.rubric, []).append(p)
    assert set(by) == {"lot", "drop", "house", "sold", "nofight"}  # неделя — по понедельникам, итоги недели — по пятницам
    texts = "\n".join(p.text for p in posts)
    assert "Машино-место на 40 % дешевле района" in texts and "Квартира на 40 % дешевле района за м²" in texts
    assert "📉 <b>Город снизил стартовую цену на 25 %</b>" in texts and "2,00 млн ₽ → <b>1,50 млн ₽</b>" in texts
    assert "🏢 <b>5 квартир в одном доме</b>" in texts
    assert "Машино-место продано в 2,9 раза" in texts and "Квартира продана в 1,3 раза" in texts
    assert "квартира продана по стартовой цене" in texts and "ещё 1 квартира" in texts
    assert "Подольск" not in texts and "5,0 раза" not in texts
    assert "Парк культуры (7 мин пешком, 0,5 км)" in texts and "Торги 2 октября" in texts
    flat_lot = next(p for p in by["lot"] if p.card["type"] == "flat")
    assert flat_lot.card["headline"] == "−40%" and flat_lot.card["headline_sub"] == "дешевле района за м²"
    assert flat_lot.card["template"] == "lot" and flat_lot.card["brand"] == "ПОПУТНЫЙ МЕТР"
    assert by["sold"][0].card["headline"] == "×2,9" and by["nofight"][0].card["headline"] == "+0 ₽"

    registry = {p.key: "2026-10-06" for p in posts}
    assert content.drafts(con, tmp_path, tuesday, results_path=res, registry=registry) == []


def test_week_and_weekstats(con, tmp_path):
    journal(con, tmp_path)
    policy = dict(content.POLICY, week_min_lots=1)
    week = content.drafts(con, tmp_path, date(2026, 10, 5), rubrics=("week",), policy=policy)
    assert [p.key for p in week] == ["week:parking:2026-W41"]
    assert "Неделя вперёд: машино-места" in week[0].text and "ул. Дешёвая, 1" in week[0].text
    assert week[0].card["template"] == "list" and week[0].card["rows"][0]["lead"] == "−40%"
    stats = content.drafts(con, tmp_path, date(2026, 10, 8), results_path=results_csv(tmp_path), rubrics=("weekstats",))
    assert [p.key for p in stats] == ["weekstats:parking:2026-W41", "weekstats:flat:2026-W41"]
    park = stats[0]
    assert "Продано: 2 из 3 торгов" in park.text and "без торга: 1 (50 %)" in park.text
    assert park.card["stats"][0]["num"] == "50%" and park.card["stats"][1]["num"] == "×2,9"
    assert park.card["headline"] == "2" and park.card["deadline"] == "2–8 октября"


def test_text_is_html_escaped():
    r = {"kind": "parking", "place": "ул. <Тест> & Ко", "district": "район А", "price": 1e6, "ref": 2e6,
         "scope": "района", "discount": 0.5, "space": 10, "at_addr": 1, "end_d": date(2026, 11, 1),
         "url": "https://x.test/?a=1&b=2"}
    t = templates.lot(r)
    assert "ул. &lt;Тест&gt; &amp; Ко" in t and 'href="https://x.test/?a=1&amp;b=2"' in t
    assert templates.money(504_000) == "504 тыс. ₽" and templates.money(1_293_842) == "1,29 млн ₽"


class FakeTransport:
    def __init__(self, responses=None):
        self.calls, self.responses = [], list(responses or [])

    def __call__(self, url, body, ctype):
        self.calls.append((url.rsplit("/", 1)[-1], body))
        if self.responses:
            return self.responses.pop(0)
        return 200, b'{"ok": true, "result": {}}'


def test_bot_caption_split_and_retry(tmp_path):
    png = tmp_path / "c.png"
    png.write_bytes(b"\x89PNG fake")
    t = FakeTransport()
    bot = telegram.Bot("T", transport=t, sleep=lambda s: None)
    bot.send_post("@kitchen", "коротко", png)
    assert [c[0] for c in t.calls] == ["sendPhoto"] and "коротко".encode() in t.calls[0][1]
    t.calls.clear()
    bot.send_post("@kitchen", "x" * 2000, png)
    assert [c[0] for c in t.calls] == ["sendPhoto", "sendMessage"]

    t = FakeTransport()
    telegram.Bot("T", transport=t).call("getMe", {})
    assert t.calls == [("getMe", b"")]                 # без параметров — пустое тело, не пустой multipart

    t = FakeTransport([(429, b'{"ok": false, "parameters": {"retry_after": 1}}')])
    telegram.Bot("T", transport=t, sleep=lambda s: None).send_post("@kitchen", "текст")
    assert [c[0] for c in t.calls] == ["sendMessage", "sendMessage"]
    with pytest.raises(telegram.TelegramError):
        telegram.Bot("T", transport=FakeTransport([(400, b'{"ok": false, "description": "Bad Request"}')])
                     ).send_post("@kitchen", "текст")


def test_cli_channel_draft_sends_and_remembers(con, tmp_path, monkeypatch):
    from parkan.cli import main
    journal(con, tmp_path / "state")
    t = FakeTransport()
    monkeypatch.setattr(telegram, "urllib_transport", t)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_DRAFT_CHAT", "@kitchen")
    args = ["channel-draft", "--state-dir", str(tmp_path / "state"), "--results", str(results_csv(tmp_path)),
            "--out", str(tmp_path / "out"), "--channel-state", str(tmp_path / "ch"), "--today", "2026-10-06", "--send"]
    assert main(args) == 0
    day = tmp_path / "out" / "2026-10-06"
    manifest = json.loads((day / "drafts.json").read_text(encoding="utf-8"))
    assert {m["rubric"] for m in manifest} == {"lot", "drop", "house", "sold", "nofight"}
    assert (day / "01-lot.html").exists() and len(t.calls) == len(manifest)
    if html_cards.available():
        assert all(m["card"] for m in manifest)
    assert set(json.loads((tmp_path / "ch" / "drafted.json").read_text())) == {m["key"] for m in manifest}
    t.calls.clear()
    assert main(args) == 0 and t.calls == []          # повторный запуск не шлёт то же самое


def test_html_card_map_and_district_line():
    from parkan.channel import html_cards as h
    assert h.district_line("Бескудниковский район") == "Бескудниковский район · САО"
    assert h.district_line("район Хамовники") == "район Хамовники · ЦАО"
    assert h.district_line("район Нигде") == "район Нигде"           # не нашли — как есть, без округа
    svg = h.map_svg("район Северное Измайлово")
    assert svg.count("<path") > 100 and h.LIME in svg
    html = h.lot_html({"kicker": "ЛОТ ДНЯ", "headline": "−44%", "address": "ул. <А>", "district": "Хамовники"})
    assert "ул. &lt;А&gt;" in html and "район Хамовники · ЦАО" in html


@pytest.mark.skipif(not __import__("parkan.channel.html_cards", fromlist=["x"]).available(), reason="нет Chromium")
def test_html_card_renders(tmp_path):
    from PIL import Image
    from parkan.channel import html_cards as h
    out = h.render_html(h.lot_html({"kicker": "ЛОТ ДНЯ", "headline": "−44%", "price": "1,29 млн ₽",
                                    "district": "Бескудниковский район"}), tmp_path / "c.png")
    im = Image.open(out)
    assert im.size == (1080, 1080) and im.getpixel((5, 5)) == (14, 59, 46)   # фон #0E3B2E
