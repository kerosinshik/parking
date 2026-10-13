import json
from datetime import date

from parkan.channel import market_check as mc
from parkan.channel import telegram

from .test_channel import FakeTransport

LOT = {"address": "Москва, Чоботовская улица, дом 2, корпус 1", "place": "Чоботовская улица, дом 2, корпус 1",
       "district": "район Ново-Переделкино", "price": 17_080_000, "space": 70.5, "url": "https://torgi.mos.ru/tender/1",
       "end_d": "2026-11-10", "trades_d": "2026-11-19", "coords": "55.65,37.36"}


def test_parse_listings_formats_and_errors():
    items, errors = mc.parse_listings(
        "Нашёл вот что:\n"
        "https://www.cian.ru/sale/flat/1/ 24,5 млн 70\n"
        "https://www.avito.ru/moskva/kvartiry/2 23 900 000 ₽ 68,5 м2\n"
        "https://cian.ru/sale/flat/3/ 72 м² 25.2м\n"
        "https://cian.ru/sale/flat/4/\n"
        "https://cian.ru/sale/flat/5/ 2,5 млн 70\n")
    assert [(x["site"], round(x["m2"])) for x in items] == [("ЦИАН", 350000), ("Авито", 348905), ("ЦИАН", 350000)]
    assert len(errors) == 2 and "строка 5: нужна цена и площадь" in errors[0] and "строка 6: выходит" in errors[1]


def test_search_links_and_markup():
    links = mc.search_links(LOT)
    assert links[0][0] == "ЦИАН — рядом" and "center=55.65,37.36" in links[0][1] and "minarea=56&maxarea=85" in links[0][1]
    assert links[1][1].endswith("q=%D0%A7%D0%BE%D0%B1%D0%BE%D1%82%D0%BE%D0%B2%D1%81%D0%BA%D0%B0%D1%8F%20"
                                "%D1%83%D0%BB%D0%B8%D1%86%D0%B0")          # «Чоботовская улица»
    kb = json.loads(mc.reply_markup(LOT))["inline_keyboard"][0]
    assert [b["text"] for b in kb] == ["ЦИАН — рядом", "Авито — улица"]
    assert mc.lot_for_market({"kind": "parking", "price": 1, "space": 13}) is None


def test_compare_facts_card():
    items, _ = mc.parse_listings("https://cian.ru/1 24,5 млн 70\nhttps://cian.ru/2 26,6 млн 70\nhttps://avito.ru/3 22,4 млн 70")
    c = mc.compare(LOT, items)
    f = mc.facts(LOT, items, c, date(2026, 10, 13))["facts"]
    assert f["listings_count"] == "3 объявления" and f["listings_range"] == "320–380 тыс. ₽/м²"
    assert f["listings_median"] == "350 000 ₽/м²" and f["below_listings_median"] == "31 %"
    assert f["listings_sites"] == "Авито, ЦИАН" and len(f["listings"]) == 3 and "не цены сделок" in f["listings_what"]
    card = mc.card(LOT, c)
    assert card["kicker"] == "РЯДОМ НА РЫНКЕ" and card["headline"] == "−31%" and "320–380 тыс. ₽/м² (3)" in card["price_sub"]


def _send(tmp_path, monkeypatch, responses):
    t = FakeTransport(responses)
    monkeypatch.setattr(telegram, "urllib_transport", t)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_DRAFT_CHAT", "-100500")
    return t


def test_send_remembers_and_channel_market_builds_draft(tmp_path, monkeypatch):
    from parkan.channel.commands import send_dir
    from parkan.cli import main
    day = tmp_path / "out" / "2026-10-13"
    day.mkdir(parents=True)
    (day / "01-torgi.html").write_text("текст", encoding="utf-8")
    (day / "01-torgi.facts.json").write_text("{}", encoding="utf-8")
    (day / "drafts.json").write_text(json.dumps([{"key": "torgi:x", "rubric": "torgi", "facts": "01-torgi.facts.json",
                                                  "template": "01-torgi.html", "copy": "01-torgi.txt", "card": None,
                                                  "sent": False, "market_lot": LOT}]), encoding="utf-8")
    t = _send(tmp_path, monkeypatch, [(200, b'{"ok": true, "result": {"message_id": 77}}')])
    state = tmp_path / "ch"
    assert send_dir(day, state / "drafted.json", None, date(2026, 10, 13)) == 0
    assert b"inline_keyboard" in t.calls[0][1] and "ЦИАН — рядом".encode() in t.calls[0][1]
    kitchen = json.loads((state / "kitchen.json").read_text(encoding="utf-8"))
    assert kitchen["77"]["key"] == "torgi:x" and kitchen["77"]["lot"]["price"] == 17_080_000

    updates = {"ok": True, "result": [
        {"update_id": 5, "channel_post": {"message_id": 90, "chat": {"id": -100500},
                                          "reply_to_message": {"message_id": 77},
                                          "text": "https://cian.ru/1 24,5 млн 70\nhttps://avito.ru/3 22,4 млн 70"}},
        {"update_id": 6, "channel_post": {"message_id": 91, "chat": {"id": -100500},
                                          "reply_to_message": {"message_id": 77}, "text": "https://cian.ru/9 без цены"}},
        {"update_id": 7, "channel_post": {"message_id": 92, "chat": {"id": -100500}, "text": "просто заметка"}}]}
    t = _send(tmp_path, monkeypatch, [(200, json.dumps(updates).encode())])
    args = ["channel-market", "--out", str(tmp_path / "mk"), "--channel-state", str(state), "--today", "2026-10-13"]
    assert main(args) == 0
    assert [c[0] for c in t.calls] == ["getUpdates", "sendMessage"]          # ответ про строку без цены
    assert "нужна цена и площадь".encode() in t.calls[1][1]
    mday = tmp_path / "mk" / "2026-10-13"
    manifest = json.loads((mday / "drafts.json").read_text(encoding="utf-8"))
    assert [m["key"] for m in manifest] == ["market:torgi:x:90"]
    facts = json.loads((mday / "01-market.facts.json").read_text(encoding="utf-8"))
    assert facts["rubric"] == "market" and facts["facts"]["listings_count"] == "2 объявления"
    assert json.loads((state / "updates.json").read_text())["offset"] == 8

    t = _send(tmp_path, monkeypatch, [(200, b'{"ok": true, "result": []}')])
    assert main(args) == 0                                                   # повторный запуск ничего не дублирует
    assert b"offset" in t.calls[0][1] and len(json.loads((mday / "drafts.json").read_text())) == 1
