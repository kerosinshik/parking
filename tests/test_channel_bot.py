import json
from datetime import date, datetime

from pathlib import Path

from parkan.channel import bot, bot_media
from parkan.channel.telegram import Bot, TelegramError

from .test_channel import con, journal, results_csv  # noqa: F401


class Recorder:
    """Подмена Bot: запоминает вызовы, getChatMember отвечает по списку подписчиков."""

    def __init__(self, members=(), broken=False, blocked=()):
        self.calls, self.members, self.broken, self.blocked = [], set(members), broken, set(blocked)

    def call(self, method, fields, files=None):
        if method == "sendMessage" and fields["chat_id"] in self.blocked:
            raise TelegramError("sendMessage: Forbidden: bot was blocked by the user")
        self.calls.append((method, fields))
        if method == "getChatMember":
            if self.broken:
                raise TelegramError("getChatMember: member list is inaccessible")
            return {"status": "member" if fields["user_id"] in self.members else "left"}
        return {}

    def sent(self):
        return [f for m, f in self.calls if m == "sendMessage"]

    def last(self):
        f = self.sent()[-1]
        kb = json.loads(f["reply_markup"])["inline_keyboard"] if f.get("reply_markup") else []
        return f["text"], [b for row in kb for b in row]


def snapshot(tmp_path, lots=None, sold=None, day="2026-10-06"):
    p = tmp_path / "lots.json"
    p.write_text(json.dumps({"date": day, "lots": lots or [], "sold": sold or []}, ensure_ascii=False), encoding="utf-8")
    return p


def L(i, kind="parking", adm="ЦАО", discount=0.4, place=None, end="2026-10-20"):
    return {"id": str(i), "kind": kind, "place": place or f"ул. {i}", "district": "район Хамовники", "adm": adm,
            "price": 1_200_000, "space": 13.3, "ref": 2_000_000, "scope": "района", "discount": discount,
            "end_d": end, "trades_d": "2026-10-25", "url": f"https://torgi.mos.ru/tender/{i}"}


SOLD = [{"id": "1", "kind": "parking", "place": "улица Льва Толстого, дом 5", "district": "район Хамовники",
         "space": 14.1, "start": 1_000_000, "final": 2_900_000, "date": "2026-10-02", "url": "https://torgi.mos.ru/tender/1"}]


def make(tmp_path, rec, lots=None, sold=None, **kw):
    snap = bot.Snapshot(snapshot(tmp_path, lots, sold))
    store = bot.Store(":memory:")
    kw.setdefault("now", lambda: datetime(2026, 10, 6, 12))
    return bot.Gate(rec, snap, store, "https://t.me/poputny_metr", log=lambda m: None, **kw), store


def msg(text, uid=7):
    return {"message": {"chat": {"id": uid, "type": "private"}, "from": {"id": uid, "first_name": "А"}, "text": text}}


def press(data, uid=7):
    return {"callback_query": {"id": "q", "from": {"id": uid}, "data": data,
                               "message": {"chat": {"id": uid}}}}


def test_export_snapshot(con, tmp_path):  # noqa: F811
    journal(con, tmp_path)
    out = tmp_path / "bot" / "lots.json"
    data = bot.export(con, tmp_path, date(2026, 10, 6), out, results_path=results_csv(tmp_path))
    assert json.loads(out.read_text(encoding="utf-8")) == data
    cheap = {l["place"]: l for l in data["lots"]}
    assert cheap["ул. Дешёвая, 1"]["adm"] == "ЦАО" and cheap["ул. Дешёвая, 1"]["end_d"] == "2026-10-09"
    assert "Подольск" not in json.dumps(data, ensure_ascii=False)
    assert all(l["discount"] >= bot.EXPORT_MIN_DISCOUNT for l in data["lots"])
    assert {s["id"] for s in data["sold"]} == {"1", "2", "4", "11", "12", "13"}   # без Подмосковья и несостоявшихся


def test_welcome_source_and_gate(tmp_path):
    rec = Recorder(members={8})
    g, store = make(tmp_path, rec, [L(i) for i in range(6)])
    g.handle(msg("/start vc_article"))
    text, kb = rec.last()
    assert "машино-мест — 6" in text and {b.get("callback_data") for b in kb} >= {"parking", "flat", "g", "alerts"}
    assert store.q("SELECT source FROM users WHERE id = 7") == [("vc_article",)]
    g.handle(msg("/start other"))        # метка — только первая
    assert store.q("SELECT source FROM users WHERE id = 7") == [("vc_article",)]

    g.handle(press("parking"))           # не подписан: два лота и кнопка подписки
    text, kb = rec.last()
    assert "1. " in text and "2. " in text and "3. " not in text and "для подписчиков" in text
    assert kb[0]["url"] == "https://t.me/poputny_metr" and kb[1]["callback_data"] == "parking:"
    g.handle(press("parking", uid=8))    # подписан: пять лотов и округа
    text, kb = rec.last()
    assert "5. " in text and "6. " not in text and "ЦАО" in {b["text"] for b in kb}
    assert ("getChatMember", {"chat_id": "@poputny_metr", "user_id": 8}) in rec.calls
    n = len(rec.calls)
    g.handle(press("parking", uid=8))    # подписку не проверяем на каждое нажатие
    assert "getChatMember" not in [m for m, _ in rec.calls[n:]]


def test_gate_fails_open_and_groups_ignored(tmp_path):
    rec = Recorder(broken=True)
    g, _ = make(tmp_path, rec, [L(i) for i in range(6)])
    g.handle(press("parking"))
    assert "5. " in rec.last()[0]
    n = len(rec.sent())
    g.handle({"message": {"chat": {"id": -1, "type": "supergroup"}, "from": {"id": 7}, "text": "/start"}})
    assert len(rec.sent()) == n


def test_lot_deeplink(tmp_path):
    rec = Recorder(members={7})
    g, _ = make(tmp_path, rec, [L(42, kind="flat", adm="ЮЗАО")])
    g.handle(msg("/start lot_42"))
    text, kb = rec.last()
    assert "Квартира на 40 % дешевле района за м²" in text and "tender/42" in text
    assert kb[0]["callback_data"] == "flat:ЮЗАО" and kb[1]["callback_data"] == "a+:flat:ЮЗАО"
    g.handle(msg("/start lot_999"))
    assert "уже нет" in rec.last()[0]


def test_alerts_and_notify(tmp_path):
    rec = Recorder(members={7, 9})
    g, store = make(tmp_path, rec, [L(1)])
    g.tick()                             # первый снимок — точка отсчёта
    assert rec.sent() == []
    g.handle(press("a+:parking:ЦАО"))
    assert store.alerts(7) == [("parking", "ЦАО")] and "Готово" in rec.last()[0]
    g.handle(press("a+:parking:", uid=9))
    rec.blocked.add(9)                   # потом остановил бота
    g.handle(press("a+:flat:Кремль"))    # чужой округ из подделанной кнопки
    assert store.alerts(7) == [("parking", "ЦАО")]

    snapshot(tmp_path, [L(1), L(2), L(3, adm="САО"), L(4, kind="flat")])
    import os
    os.utime(tmp_path / "lots.json", (1, 2))     # mtime точно другой
    n = len(rec.sent())
    g.tick()
    new = rec.sent()[n:]
    assert [f["chat_id"] for f in new] == [7]    # 9 заблокировал бота
    assert "ул. 2" in new[0]["text"] and "ул. 3" not in new[0]["text"] and "ул. 4" not in new[0]["text"]
    assert store.alerts(9) == [] and store.q("SELECT blocked FROM users WHERE id = 9") == [(1,)]
    g.tick()                                     # тот же снимок — повторно не шлём
    assert len(rec.sent()) == n + 1

    g.handle(press("a-:parking:ЦАО"))
    assert store.alerts(7) == []


def test_game(tmp_path):
    rec = Recorder()
    g, store = make(tmp_path, rec, sold=SOLD)
    g.handle(msg("/game"))
    text, kb = rec.last()
    assert "Стартовая цена — <b>1,00 млн ₽</b>" in text and len(kb) == 3
    opts, right = bot.game_options(SOLD[0])
    assert kb[right]["text"] == "2,90 млн ₽" and len(set(b["text"] for b in kb)) == 3
    g.handle(press(kb[right]["callback_data"]))
    text, _ = rec.last()
    assert text.startswith("<b>Верно!</b>") and "в 2,9 раза" in text and "Счёт: 1 из 1" in text
    g.handle(press(kb[right]["callback_data"]))   # повторное нажатие не засчитывается
    assert store.score(7)["played"] == 1
    assert bot.game_options(SOLD[0]) == (opts, right)   # варианты одинаковы для лота


def test_stats_only_for_admins(tmp_path):
    rec = Recorder()
    g, _ = make(tmp_path, rec, [L(1)], admins=[1])
    g.handle(msg("/stats", uid=7))
    assert "Пользователи бота" not in rec.last()[0]
    g.handle(msg("/stats", uid=1))
    assert "Пользователи бота</b>: 2" in rec.last()[0]


def test_setup_and_run_loop(tmp_path):
    class Fake(Recorder):
        def __init__(self):
            super().__init__()
            self.polls = [[{"update_id": 5, **msg("/start")}], TelegramError("сеть"), []]

        def call(self, method, fields, files=None):
            if method == "getUpdates":
                self.calls.append((method, fields))
                r = self.polls.pop(0)
                if isinstance(r, Exception):
                    raise r
                return r
            return super().call(method, fields, files)

    rec = Fake()
    g, _ = make(tmp_path, rec, [L(1)])
    bot.run(g, rec, stop=lambda: not rec.polls, sleep=lambda s: None)
    methods = [m for m, _ in rec.calls]
    assert methods[:3] == ["setMyDescription", "setMyShortDescription", "setMyCommands"]
    polls = [f for m, f in rec.calls if m == "getUpdates"]
    assert polls[1]["offset"] == 6 and "sendMessage" in methods
    assert len(rec.calls[0][1]["description"]) <= 512


def test_channel_chat_and_env(tmp_path):
    assert bot.channel_chat("https://t.me/poputny_metr") == "@poputny_metr"
    assert bot.channel_chat("https://example.com/x") is None
    g, tg = bot.from_env({"PARKAN_BOT_TOKEN": "T", "PARKAN_BOT_DB": ":memory:", "PARKAN_BOT_ADMINS": "1, 2",
                          "PARKAN_BOT_LOTS": str(tmp_path / "none.json")})
    assert isinstance(tg, Bot) and g.admins == {1, 2} and g.chat == "@poputny_metr" and g.gate


class Photos(Recorder):
    """Recorder, у которого sendPhoto возвращает file_id."""

    def call(self, method, fields, files=None):
        r = super().call(method, fields, files)
        if method == "sendPhoto":
            if getattr(self, "photo_fails", False):
                raise TelegramError("sendPhoto: Bad Request")
            return {"photo": [{"file_id": "small"}, {"file_id": "BIG"}]}
        return r


def fake_cards(tmp_path, drawn):
    def render(card, path):
        drawn.append(card)
        Path(path).write_bytes(b"png")
    return bot_media.Cards(tmp_path / "cards", render=render, available=lambda: True, sync=True, log=lambda m: None)


def test_cards_photo_caption_file_id_and_fallback(tmp_path):
    drawn = []
    rec = Photos(members={7})
    g, _ = make(tmp_path, rec, [L(i) for i in range(6)], sold=SOLD, cards=fake_cards(tmp_path, drawn))
    g.handle(msg("/start"))
    method, f = rec.calls[-1]
    assert method == "sendPhoto" and "Город продаёт" in f["caption"] and f["parse_mode"] == "HTML"
    assert drawn[-1]["template"] == "cover" and json.loads(f["reply_markup"])["inline_keyboard"]
    g.handle(msg("/start"))                       # второй раз — по file_id, без загрузки и отрисовки
    assert rec.calls[-1][1]["photo"] == "BIG" and len(drawn) == 1

    g.handle(press("parking"))                    # подборка: карточка-список
    assert rec.calls[-1][0] == "sendPhoto" and drawn[-1]["template"] == "list" and drawn[-1]["headline"] == "6"
    g.handle(msg("/start lot_3"))
    assert drawn[-1]["kicker"] == "ЛОТ НА ТОРГАХ" and drawn[-1]["headline"] == "−40%"
    g.handle(msg("/game"))
    assert drawn[-1]["kicker"] == "УГАДАЙ ЦЕНУ" and drawn[-1]["headline"] == "?"
    assert "2,90" not in json.dumps(drawn[-1], ensure_ascii=False)   # итог в вопросе не виден

    long = "x" * 1500                             # длиннее подписи: фото, потом текст с кнопками
    g.reply(7, long, [[{"text": "a", "callback_data": "b"}]], "long", lambda: {"template": "cover"})
    assert [m for m, _ in rec.calls[-2:]] == ["sendPhoto", "sendMessage"] and "caption" not in rec.calls[-2][1]

    rec.photo_fails = True                        # фото не ушло — ответ текстом
    g.reply(7, "текст", None, "other", lambda: {"template": "cover"})
    assert rec.calls[-1][0] == "sendMessage" and rec.calls[-1][1]["text"] == "текст"


def test_cards_off_and_prewarm(tmp_path):
    rec = Recorder()
    off = bot_media.Cards(tmp_path / "c", available=lambda: False)
    g, _ = make(tmp_path, rec, [L(1)], cards=off)
    g.handle(msg("/start"))
    assert rec.calls[-1][0] == "sendMessage" and not (tmp_path / "c").exists()

    drawn = []
    g, _ = make(tmp_path, Recorder(), [L(1), L(2, adm="САО"), L(3, kind="flat")], sold=SOLD,
                cards=fake_cards(tmp_path, drawn))
    g.tick()
    kinds = [c["template"] for c in drawn]
    assert kinds.count("cover") == 1 and kinds.count("list") >= 4 and "lot" in kinds
    assert any(c["kicker"] == "УГАДАЙ ЦЕНУ" for c in drawn)
    n = len(drawn)
    g.tick()                                      # тот же снимок — заново не рисуем
    assert len(drawn) == n


def test_house_link_open_to_all(tmp_path):
    rec = Recorder()                              # не подписан
    house = {"id": "77", "kind": "parking", "place": "проезд Черепановых, дом 10", "district": "район Коптево",
             "adm": "САО", "n": 186, "price_min": 2_060_000, "price_max": 2_780_000, "space_min": 13.3,
             "space_max": 21.6, "ref": None, "scope": "района", "end_d": "2026-10-15", "trades_d": "2026-10-21",
             "url": "https://torgi.mos.ru/tender/77"}
    g, store = make(tmp_path, rec)
    p = tmp_path / "lots.json"
    p.write_text(json.dumps({"date": "2026-10-06", "lots": [L(1)], "houses": [house]}, ensure_ascii=False),
                 encoding="utf-8")
    g.handle(msg("/start dom_77"))
    text, kb = rec.last()
    assert "186 машино-мест в одном доме" in text and "от 2,06 млн ₽ до 2,78 млн ₽" in text and "Медиана" not in text
    assert kb[0]["url"] == "https://t.me/poputny_metr" and kb[1]["callback_data"] == "a+:parking:САО"
    assert store.q("SELECT source FROM users WHERE id = 7") == [("dom_77",)]
    g.handle(msg("/start lot_1"))                 # карточка лота тоже открыта, но с приглашением в канал
    text, kb = rec.last()
    assert "дешевле района" in text and "для подписчиков" in text and kb[0]["url"] == "https://t.me/poputny_metr"
    g.handle(msg("/start dom_999"))               # дома нет — ищем лот, нет и его — меню
    assert "уже нет" in rec.last()[0]


def test_post_signature_links_to_bot(monkeypatch):
    from parkan.channel.commands import bot_link, with_signature
    monkeypatch.delenv("PARKAN_CHANNEL_URL", raising=False)
    monkeypatch.setenv("PARKAN_BOT_URL", "https://t.me/torgi_bot")
    lot = {"rubric": "lot", "facts": {"lot_url": "https://torgi.mos.ru/tender/123"}}
    assert bot_link(lot) == "https://t.me/torgi_bot?start=lot_123"
    assert bot_link({"rubric": "house", "facts": {"cheapest_lot_url": "https://torgi.mos.ru/tender/9"}}) \
        == "https://t.me/torgi_bot?start=dom_9"
    assert bot_link({"rubric": "sold", "facts": {"lot_url": "https://torgi.mos.ru/tender/5"}}) \
        == "https://t.me/torgi_bot?start=ch_sold"
    text = with_signature("Текст", lot)
    assert text.endswith('<a href="https://t.me/poputny_metr">Попутный Метр</a> · '
                         '<a href="https://t.me/torgi_bot?start=lot_123">Карточка лота и похожие — в боте</a>')
    monkeypatch.setenv("PARKAN_BOT_URL", "")
    assert with_signature("Текст", lot).endswith(">Попутный Метр</a>")


def test_houses_report(con, tmp_path):  # noqa: F811
    from parkan.channel import houses
    journal(con, tmp_path)
    found = houses.find(con, tmp_path, date(2026, 10, 6), {"parking": 5, "flat": 3})
    by = {h["place"]: h for h in found}
    assert by["ул. Новая, 3"]["n"] == 5 and by["ул. Новая, 3"]["kind"] == "flat"
    text = houses.report(found, "https://t.me/torgi_bot", date(2026, 10, 6))
    assert "город продаёт на торгах 5 квартир" in text and f"?start=dom_{by['ул. Новая, 3']['lot_id']}" in text
    assert "Подольск" not in text
    data = bot.export(con, tmp_path, date(2026, 10, 6), tmp_path / "s.json")
    assert any(h["place"] == "ул. Новая, 3" for h in data["houses"])
