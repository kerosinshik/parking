import json
from datetime import date, datetime

import duckdb
import pytest

from parkan.channel import cards, content, telegram, templates
from parkan.track import digest, torgi
from parkan.track.sync import sync_dataset

from .test_track import LOTS, FakeClient


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    yield c
    c.close()


def lot(i, price, addr, start="01.10.2026", end="30.10.2026"):
    return {"global_id": 7000 + i, "ID": i, "ObjectType": "машино-место", "Stage": "опубликовано",
            "StartPrice": price, "Space": 13.3, "District": "район Пресненский", "Address": addr,
            "StartReceptionDate": start, "EndReceptionDate": end, "TradesDate": "05.11.2026",
            "WebSite": [{"WebSite": f"https://torgi.mos.ru/tender/{i}"}]}


def journal(con, tmp_path):
    """Два дня журнала: на второй — новый лот на 40 % дешевле района и снижение цены у старого."""
    lots = [lot(i, 2_000_000 + i * 1000, f"ул. {i}") for i in range(8)]
    sync_dataset(con, FakeClient({1461: lots}), LOTS, tmp_path, observed_at=datetime(2026, 10, 5, 7))
    lots.append(lot(100, 1_200_000, "ул. Дешёвая, 1", start="05.10.2026", end="09.10.2026"))
    lots[0]["StartPrice"] = 1_500_000
    sync_dataset(con, FakeClient({1461: lots}), LOTS, tmp_path, observed_at=datetime(2026, 10, 6, 7))


def results_csv(tmp_path):
    rows = {
        "1": {"tender_id": "1", "trades_date": "2026-10-02", "district": "район Хамовники",
              "address": "Российская Федерация, город Москва, внутригородская территория муниципальный округ "
                         "Хамовники, улица Льва Толстого, дом 5", "space_m2": "14.1", "start_price": "1000000",
              "final_price": "2900000", "final_to_start": "2.9", "final_price_per_m2": "205674",
              "status": "Признаны состоявшимися", "subway": "Парк культуры (7 мин пешком, 0.5 км)",
              "url": "https://torgi.mos.ru/tender/1", "checked_at": "2026-10-05 07:00"},
        "2": {"tender_id": "2", "trades_date": "2026-10-02", "start_price": "1000000", "final_price": "1050000",
              "final_to_start": "1.05", "status": "Признаны состоявшимися", "checked_at": "2026-10-05 07:00"},
        "3": {"tender_id": "3", "trades_date": "2026-10-02", "status": "Признаны несостоявшимися",
              "checked_at": "2026-10-05 07:00"},
        "4": {"tender_id": "4", "trades_date": "2026-08-02", "start_price": "1000000", "final_price": "3000000",
              "final_to_start": "3.0", "status": "Признаны состоявшимися", "checked_at": "2026-08-05 07:00"},
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


def test_drafts_rubrics_and_registry(con, tmp_path):
    journal(con, tmp_path)
    res = results_csv(tmp_path)
    tuesday = date(2026, 10, 6)
    posts = content.drafts(con, tmp_path, tuesday, results_path=res)
    assert [p.rubric for p in posts] == ["lot", "lot", "result"]          # «неделя» — только по понедельникам
    cheap = next(p for p in posts if "дешевле района" in p.text)
    assert "ул. Дешёвая, 1 (Пресненский)" in cheap.text and "1,20 млн ₽" in cheap.text
    assert '<a href="https://torgi.mos.ru/tender/100">' in cheap.text and templates.EDITOR in cheap.text
    assert cheap.card["big"] == "−40 %" and cheap.card["badge"] == "заявки до 09.10.2026"
    result = posts[-1]
    assert result.key == "result:1" and "в 2,9 раза" in result.text and "улица Льва Толстого, дом 5" in result.text
    assert "Парк культуры (7 мин пешком, 0,5 км)" in result.text and result.card["big"] == "×2,9"
    assert "Торги 02.10.2026" in result.text and result.card["badge"] == "торги 02.10.2026"

    registry = {p.key: "2026-10-06" for p in posts}
    assert content.drafts(con, tmp_path, tuesday, results_path=res, registry=registry) == []

    monday = content.drafts(con, tmp_path, date(2026, 10, 5), rubrics=("week",))
    assert len(monday) == 1 and monday[0].key == "week:2026-W41"
    assert "ул. Дешёвая, 1" in monday[0].text and "до 09.10.2026" in monday[0].text


def test_text_is_html_escaped():
    f = digest.Finding("lots", "дешевле района", "", "", 0.9, data={
        "address": "ул. <Тест> & Ко", "district": "район А", "price": 1e6, "median": 2e6, "discount": 0.5,
        "space": 10, "m2": 100000, "lots": 1, "until": "01.11.2026", "url": "https://x.test/?a=1&b=2"})
    t = templates.lot(f)
    assert "ул. &lt;Тест&gt; &amp; Ко" in t and 'href="https://x.test/?a=1&amp;b=2"' in t


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

    t = FakeTransport([(429, b'{"ok": false, "parameters": {"retry_after": 1}}')])
    telegram.Bot("T", transport=t, sleep=lambda s: None).send_post("@kitchen", "текст")
    assert [c[0] for c in t.calls] == ["sendMessage", "sendMessage"]
    with pytest.raises(telegram.TelegramError):
        telegram.Bot("T", transport=FakeTransport([(400, b'{"ok": false, "description": "Bad Request"}')])
                     ).send_post("@kitchen", "текст")


@pytest.mark.skipif(not cards.available(), reason="нет Pillow или шрифта с кириллицей")
def test_card_renders(tmp_path):
    from PIL import Image
    card = {"label": "ЛОТ ДНЯ", "big": "−40 %", "sub": "1,20 млн ₽ при медиане района 2,00 млн ₽",
            "place": "очень длинный адрес " * 8, "district": "Пресненский", "badge": "заявки до 09.10.2026"}
    out = cards.render(card, tmp_path / "c.png", "Попутный Метр")
    assert Image.open(out).size == (cards.W, cards.H)


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
    assert [m["rubric"] for m in manifest] == ["lot", "lot", "result"] and (day / "01-lot.html").exists()
    assert len(t.calls) >= 3
    assert set(json.loads((tmp_path / "ch" / "drafted.json").read_text())) == {m["key"] for m in manifest}
    t.calls.clear()
    assert main(args) == 0 and t.calls == []          # повторный запуск не шлёт то же самое
