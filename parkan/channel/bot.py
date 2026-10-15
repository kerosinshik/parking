"""Бот-дорвей канала: вход из поиска Telegram и по ссылкам, подборки лотов — подписчикам канала.

Зачем: боты находятся в глобальном поиске Telegram по имени и описанию, каналы — хуже. Человек ищет
«торги Москва», находит бота, получает пару выгодных лотов и кнопку «Подписаться». Полная подборка
(все округа, карточка лота по ссылке из поста, уведомления) — после подписки на канал: так бот приводит подписчиков.

Ради чего возвращаются (а активность — главный сигнал ранжирования бота в поиске):
- подборки по округам и карточка лота по ссылке из поста (`/start lot_<id>`);
- уведомления: «присылать новые машино-места в ЮЗАО» — бот пишет сам, когда обновился снимок;
- игра «Угадай цену» на настоящих итогах торгов: старт известен, за сколько продали? Счёт и серия.

Устройство:
- данные — снимок `channel-export` (JSON: лоты в продаже и итоги), бот только читает его и перечитывает
  при обновлении файла;
- пользователи, уведомления, счёт игры — SQLite (stdlib); `/stats` — администраторам;
- Telegram — long polling через telegram.Bot, без сторонних библиотек и без входящих портов на сервере.

Логика (Gate) не знает про сеть и часы — всё подменяется, поэтому проверяется тестами целиком.
"""

from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

from . import bot_media, houses, lots, templates
from .telegram import CAPTION_LIMIT, Bot, TelegramError

SHOW = 5                    # лотов в одном ответе
FREE = 2                    # сколько лотов показываем без подписки — «попробовать»
MEMBER_TTL = 600            # секунд: подписку не проверяем на каждое нажатие
EXPORT_MIN_DISCOUNT = 0.10  # в снимок — лоты дешевле медианы хотя бы на 10 %
GAME_DAYS = 90              # в игру — итоги за последние N дней
NOTIFY_MAX = 5              # лотов в одном уведомлении (остальные — «ещё N»)
GAME_RATIOS = (1.0, 1.15, 1.4, 1.8, 2.5, 3.5)   # варианты «итог / старт» для подсказок в игре
PREWARM_LOTS = 20           # карточек лучших лотов каждого типа рисуем заранее
PREWARM_GAME = 100          # вопросов игры с картинкой держим наготове

# Короткие имена округов: в callback_data не больше 64 байт
ADM = {
    "Центральный административный округ": "ЦАО", "Северный административный округ": "САО",
    "Северо-Восточный административный округ": "СВАО", "Восточный административный округ": "ВАО",
    "Юго-Восточный административный округ": "ЮВАО", "Южный административный округ": "ЮАО",
    "Юго-Западный административный округ": "ЮЗАО", "Западный административный округ": "ЗАО",
    "Северо-Западный административный округ": "СЗАО", "Зеленоградский административный округ": "ЗелАО",
    "Новомосковский административный округ": "НАО", "Троицкий административный округ": "ТАО",
}

DESCRIPTION = ("Торги Москвы: машино-места и квартиры от города дешевле рынка. Лоты по округам, уведомления "
               "о новых, игра «Угадай цену» на настоящих итогах аукционов. Бот канала «{name}».")
SHORT_DESCRIPTION = "Торги Москвы: машино-места и квартиры дешевле рынка. Лоты, уведомления, «Угадай цену»."
COMMANDS = [("start", "Главное меню"), ("parking", "Машино-места дешевле района"),
            ("flats", "Квартиры дешевле района"), ("game", "Угадай цену: итоги торгов"),
            ("alerts", "Мои уведомления"), ("channel", "Канал «{name}»")]
MEMBER_STATUSES = {"creator", "administrator", "member"}
COVER = "cover:v1"


# ---------------------------------------------------------------- снимок

def export(con, state_dir, today: date, path, results_path=None, min_discount: float = EXPORT_MIN_DISCOUNT) -> dict:
    """Снимок для бота: лоты в продаже дешевле медианы на min_discount и больше + проданные лоты для игры."""
    if not lots.load(con, state_dir, today):
        raise RuntimeError(f"нет журнала набора № 1461 в {state_dir} (сначала track-sync)")
    rows = lots._finish(lots._rows(con, """
        SELECT lot_id, kind, address, district, adm, price, space, ref, scope, discount, end_d, trades_d, site
        FROM ch_sale WHERE discount >= ? ORDER BY discount DESC
    """, [min_discount]))
    sale = [{"id": str(r["lot_id"]), "kind": r["kind"], "place": r["place"], "district": r["district"],
             "adm": ADM.get(r["adm"] or "", r["adm"]), "price": r["price"], "space": r["space"], "ref": r["ref"],
             "scope": r["scope"], "discount": round(r["discount"], 4), "end_d": _iso(r["end_d"]),
             "trades_d": _iso(r["trades_d"]), "url": r["url"]} for r in rows]
    since = today - timedelta(days=GAME_DAYS)
    sold = [{"id": str(r["tender_id"]), "kind": r["kind"], "place": r["place"], "district": r.get("district"),
             "space": r.get("space_m2"), "start": r["start_price"], "final": r["final_price"],
             "date": _iso(lots.trades_date(r)), "url": r.get("url")}
            for r in lots.results(results_path)
            if r.get("final_price") and r.get("start_price") and (d := lots.trades_date(r)) and d >= since]
    homes = [{"id": str(h["lot_id"]), "kind": h["kind"], "place": h["place"], "district": h["district"],
              "adm": ADM.get(h["adm"] or "", h["adm"]), "n": h["n"], "price_min": h["price_min"],
              "price_max": h["price_max"], "space_min": h["space_min"], "space_max": h["space_max"],
              "ref": h["ref"] if houses.cheaper(h) else None, "scope": h["scope"], "end_d": _iso(h["end_d"]),
              "trades_d": _iso(h["trades_d"]), "url": h["url"]} for h in houses.query(con)]
    data = {"date": today.isoformat(), "lots": sale, "sold": sold, "houses": homes}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)   # бот не должен прочитать недописанный файл
    return data


def _iso(v) -> str | None:
    return v.isoformat() if v else None


class Snapshot:
    """Снимок с диска; перечитывается, когда файл обновился (git pull по cron)."""

    def __init__(self, path):
        self.path, self.mtime, self.date, self.lots, self.sold, self.houses = Path(path), None, None, [], [], []

    def refresh(self) -> bool:
        """True, если файл перечитан."""
        try:
            m = self.path.stat().st_mtime
        except FileNotFoundError:
            return False
        if m == self.mtime:
            return False
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.mtime, self.date = m, data.get("date")
        self.lots, self.sold, self.houses = data.get("lots", []), data.get("sold", []), data.get("houses", [])
        return True

    def open_lots(self, kind: str | None, adm: str | None, today: date) -> list[dict]:
        """Лоты, по которым ещё принимают заявки; по адресу — один, самый выгодный."""
        seen, out = set(), []
        for l in self.lots:   # уже по убыванию скидки
            if (kind and l["kind"] != kind) or (adm and l.get("adm") != adm) \
                    or (l.get("end_d") or "9") < today.isoformat():
                continue
            if (l["kind"], l["place"]) not in seen:
                seen.add((l["kind"], l["place"]))
                out.append(l)
        return out

    def find(self, lot_id: str) -> dict | None:
        return next((l for l in self.lots if l["id"] == lot_id), None)

    def find_house(self, lot_id: str) -> dict | None:
        """Дом по самому дешёвому лоту (ссылка dom_<id> из чата дома или поста «Целый дом»)."""
        return next((h for h in self.houses if h["id"] == lot_id), None)

    def find_sold(self, sid: str) -> dict | None:
        return next((s for s in self.sold if s["id"] == sid), None)

    def adms(self, kind: str, today: date) -> list[str]:
        have = {l.get("adm") for l in self.open_lots(kind, None, today)}
        return [a for a in ADM.values() if a in have]


# ---------------------------------------------------------------- хранилище

class Store:
    """Пользователи, уведомления, счёт игры, уже разосланные лоты."""

    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, source TEXT,
                started_at TEXT, last_seen TEXT, actions INTEGER DEFAULT 0, member INTEGER, blocked INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS alerts (uid INTEGER, kind TEXT, adm TEXT, PRIMARY KEY (uid, kind, adm));
            CREATE TABLE IF NOT EXISTS game (uid INTEGER PRIMARY KEY, played INTEGER DEFAULT 0,
                won INTEGER DEFAULT 0, streak INTEGER DEFAULT 0, best INTEGER DEFAULT 0, last TEXT);
            CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY);
        """)
        self.db.commit()

    def q(self, sql: str, *args) -> list:
        return self.db.execute(sql, args).fetchall()

    def x(self, sql: str, *args) -> None:
        self.db.execute(sql, args)
        self.db.commit()

    # пользователи
    def touch(self, u: dict, now: datetime, source: str | None = None) -> None:
        """Отмечает пользователя; источник — первая метка /start, потом не меняется."""
        ts = now.isoformat(timespec="seconds")
        self.x("""INSERT INTO users (id, username, first_name, source, started_at, last_seen, actions)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT (id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name,
                last_seen = excluded.last_seen, actions = actions + 1, blocked = 0,
                source = coalesce(source, excluded.source)""",
               u["id"], u.get("username"), u.get("first_name"), source, ts, ts)

    def set_member(self, uid: int, member: bool) -> None:
        self.x("UPDATE users SET member = ? WHERE id = ?", int(member), uid)

    def blocked(self, uid: int) -> None:
        """Пользователь остановил бота: уведомления ему больше не шлём."""
        self.x("UPDATE users SET blocked = 1 WHERE id = ?", uid)
        self.x("DELETE FROM alerts WHERE uid = ?", uid)

    # уведомления
    def alerts(self, uid: int) -> list[tuple[str, str]]:
        return self.q("SELECT kind, adm FROM alerts WHERE uid = ? ORDER BY kind, adm", uid)

    def add_alert(self, uid: int, kind: str, adm: str) -> None:
        self.x("INSERT OR IGNORE INTO alerts VALUES (?, ?, ?)", uid, kind, adm)

    def del_alert(self, uid: int, kind: str, adm: str) -> None:
        self.x("DELETE FROM alerts WHERE uid = ? AND kind = ? AND adm = ?", uid, kind, adm)

    def all_alerts(self) -> list[tuple[int, str, str]]:
        return self.q("SELECT uid, kind, adm FROM alerts ORDER BY uid")

    def unseen(self, ids: list[str]) -> set[str]:
        """Какие id ещё не рассылались; все переданные отмечаются как разосланные."""
        have = {r[0] for r in self.q("SELECT id FROM seen")}
        self.db.executemany("INSERT OR IGNORE INTO seen VALUES (?)", [(i,) for i in ids])
        self.db.commit()
        return set(ids) - have if have else set()   # первый снимок — точка отсчёта, без рассылки

    # игра
    def score(self, uid: int) -> dict:
        r = self.q("SELECT played, won, streak, best, last FROM game WHERE uid = ?", uid)
        return dict(zip(("played", "won", "streak", "best", "last"), r[0])) if r else \
            {"played": 0, "won": 0, "streak": 0, "best": 0, "last": None}

    def answer(self, uid: int, sid: str, ok: bool) -> dict | None:
        """Засчитывает ответ; повторное нажатие на тот же вопрос не считается (None)."""
        s = self.score(uid)
        if s["last"] == sid:
            return None
        streak = s["streak"] + 1 if ok else 0
        self.x("""INSERT INTO game (uid, played, won, streak, best, last) VALUES (?, 1, ?, ?, ?, ?)
            ON CONFLICT (uid) DO UPDATE SET played = played + 1, won = won + excluded.won,
                streak = excluded.streak, best = max(best, excluded.best), last = excluded.last""",
               uid, int(ok), streak, streak, sid)
        return self.score(uid)

    def stats(self, now: datetime) -> dict:
        week = (now - timedelta(days=7)).isoformat()
        one = lambda sql, *a: self.q(sql, *a)[0][0] or 0   # noqa: E731
        return {
            "total": one("SELECT count(*) FROM users"),
            "week_new": one("SELECT count(*) FROM users WHERE started_at >= ?", week),
            "week_active": one("SELECT count(*) FROM users WHERE last_seen >= ?", week),
            "members": one("SELECT count(*) FROM users WHERE member = 1"),
            "blocked": one("SELECT count(*) FROM users WHERE blocked = 1"),
            "alert_users": one("SELECT count(DISTINCT uid) FROM alerts"),
            "players": one("SELECT count(*) FROM game"), "rounds": one("SELECT sum(played) FROM game"),
            "sources": self.q("SELECT coalesce(source, '—'), count(*), sum(member = 1) FROM users "
                              "GROUP BY 1 ORDER BY 2 DESC LIMIT 15"),
        }


# ---------------------------------------------------------------- тексты

def channel_chat(url: str) -> str | None:
    """https://t.me/poputny_metr → @poputny_metr (для проверки подписки)."""
    m = re.match(r"^(?:https?://)?t\.me/([A-Za-z0-9_]{4,})/?$", url.strip())
    return f"@{m.group(1)}" if m else None


def _kb(rows) -> str:
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def _btn(text: str, data: str | None = None, url: str | None = None) -> dict:
    return {"text": text, "url": url} if url else {"text": text, "callback_data": data}


def _where(l: dict) -> str:
    d = templates.district(l.get("district"))
    return escape(l["place"] or "") + (f" ({escape(d)})" if d else "")


def lot_line(l: dict) -> str:
    """Строка подборки: скидка, цена, площадь, адрес, срок заявок, ссылка."""
    tail = " за м²" if l["kind"] == "flat" else ""
    bits = [f"<b>−{templates.pct(l['discount'])} %</b>{tail}", templates.money(l["price"])]
    if l.get("space"):
        bits.append(templates.area(l["space"]))
    end = [f"заявки до {templates.day(l['end_d'])}"] if l.get("end_d") else []
    if l.get("url"):
        end.append(templates.link(l["url"], "лот"))
    return " · ".join(bits) + f"\n{_where(l)}" + (f"\n{' · '.join(end)}" if end else "")


def lot_text(l: dict) -> str:
    w = templates.WORDS[l["kind"]]
    tail = " за м²" if l["kind"] == "flat" else ""
    lines = [f"<b>{w['one']} на {templates.pct(l['discount'])} % дешевле {l.get('scope') or 'района'}{tail}</b>", "",
             _where(l), f"Цена: <b>{templates.money(l['price'])}</b>"
             + (f" · {templates.area(l['space'])}" if l.get("space") else "")]
    if l.get("ref"):
        ref = templates.money(l["ref"]) if l["kind"] == "parking" else templates.rub(l["ref"]) + "/м²"
        lines.append(f"Медиана {l.get('scope') or 'района'}: {ref}")
    if l.get("end_d"):
        lines.append(f"Заявки до {templates.day(l['end_d'])}"
                     + (f", торги {templates.day(l['trades_d'])}" if l.get("trades_d") else ""))
    if l.get("url"):
        lines += ["", templates.link(l["url"], "Лот на torgi.mos.ru")]
    return "\n".join(lines)


def game_options(s: dict) -> tuple[list[float], int]:
    """Три варианта итоговой цены (один верный) и индекс верного; одни и те же для одного лота.

    Подсказки — из GAME_RATIOS, не ближе 20 % к правде, чтобы не спорить о копейках."""
    real = s["final"] / s["start"]
    rnd = random.Random(s["id"])
    decoys = sorted((r for r in GAME_RATIOS if abs(r - real) / real > 0.2), key=lambda r: abs(r - real))
    # «по стартовой» — всегда среди вариантов (так уходит большинство лотов), второй — ближайший к правде
    opts = [real] + ([1.0] + [r for r in decoys if r != 1.0][:1] if 1.0 in decoys else decoys[:2])
    rnd.shuffle(opts)
    return [s["start"] * r for r in opts], opts.index(real)


def _visible(html: str) -> str:
    """Текст без разметки — так Telegram считает длину подписи."""
    from html import unescape
    return unescape(re.sub(r"<[^>]+>", "", html))


def _alert_name(kind: str, adm: str) -> str:
    return templates.WORDS[kind]["forms"][1] + (f", {adm}" if adm else ", вся Москва")


# ---------------------------------------------------------------- логика

class Gate:
    """Обработка обновлений Telegram; всё уходит через bot.call."""

    def __init__(self, bot, snapshot: Snapshot, store: Store, channel_url: str, chat: str | None = None,
                 admins=(), gate: bool = True, clock=time.time, now=datetime.now, log=None, rnd=None,
                 cards: bot_media.Cards | None = None):
        self.bot, self.snap, self.store, self.cards = bot, snapshot, store, cards
        self.url, self.chat = channel_url, chat or channel_chat(channel_url)
        self.admins, self.gate = {int(a) for a in admins}, gate and bool(self.chat)
        self.clock, self.now, self.rnd = clock, now, rnd or random.Random()
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        self.member_cache: dict[int, float] = {}
        self.check_broken = False
        self.notified = None   # mtime снимка, по которому уже разослали уведомления

    @property
    def name(self) -> str:
        return templates.channel_name()

    # -------- Telegram

    def setup(self) -> None:
        """Описание и меню команд: по ним бот находится в поиске и понятен с первого экрана."""
        self.bot.call("setMyDescription", {"description": DESCRIPTION.format(name=self.name)})
        self.bot.call("setMyShortDescription", {"short_description": SHORT_DESCRIPTION})
        self.bot.call("setMyCommands", {"commands": json.dumps(
            [{"command": c, "description": d.format(name=self.name)} for c, d in COMMANDS], ensure_ascii=False)})

    def send(self, chat_id, text: str, kb=None) -> None:
        self.bot.call("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                      "reply_markup": _kb(kb) if kb else None,
                                      "link_preview_options": json.dumps({"is_disabled": True})})

    def reply(self, chat_id, text: str, kb=None, key: str | None = None, make=None) -> None:
        """Ответ с карточкой, если она уже нарисована; иначе текстом, а карточка — в очередь на отрисовку."""
        p = self.cards.ready(key) if self.cards and key else None
        if p is None and self.cards and key and make:
            self.cards.want(key, make, urgent=True)
            p = self.cards.ready(key)  # в синхронном режиме уже готова; в async — нет, отвечаем текстом
        if p is None:
            return self.send(chat_id, text, kb)
        short = len(_visible(text)) <= CAPTION_LIMIT
        fields = {"chat_id": chat_id}
        if short:
            fields.update(caption=text, parse_mode="HTML", reply_markup=_kb(kb) if kb else None)
        fid = self.cards.file_ids.get(key)
        try:
            res = self.bot.call("sendPhoto", dict(fields, photo=fid)) if fid else \
                self.bot.call("sendPhoto", fields, {"photo": p})
        except TelegramError as e:   # картинка не ушла — ответ всё равно нужен
            self.log(f"sendPhoto {key}: {e}")
            self.cards.file_ids.pop(key, None)
            return self.send(chat_id, text, kb)
        photos = (res or {}).get("photo") or []
        if photos:
            self.cards.file_ids[key] = photos[-1]["file_id"]
        if not short:
            self.send(chat_id, text, kb)

    def prewarm(self) -> None:
        """После обновления снимка: заранее нарисовать то, что спрашивают чаще всего."""
        if not (self.cards and self.cards.on):
            return
        self.cards.cleanup()
        self.cards.file_ids.clear()
        today = self._today()
        want = self.cards.want
        pool = [s for s in self.snap.sold if s.get("start") and s.get("final")]
        games = random.Random(self.snap.date).sample(pool, min(PREWARM_GAME, len(pool)))
        # по убыванию спроса: обложка, подборки по всей Москве, первые вопросы игры, округа, лоты, остальная игра
        want(COVER, bot_media.cover_card)
        for kind in lots.KINDS:
            for limit in (FREE, SHOW):
                key, make = self._list_card(kind, "", limit, today)
                if key:
                    want(key, make)
        for s in games[:10]:
            want(*self._game_card(s))
        for kind in lots.KINDS:
            for adm in self.snap.adms(kind, today):
                for limit in (FREE, SHOW):
                    want(*self._list_card(kind, adm, limit, today))
        for kind in lots.KINDS:
            for l in self.snap.open_lots(kind, None, today)[:PREWARM_LOTS]:
                want(*self._lot_card(l))
        for h in self.snap.houses[:PREWARM_LOTS]:
            want(f"house:{h['id']}:{h['n']}:{h['price_min']}", lambda h=h: bot_media.house_card(h))
        for s in games[10:]:
            want(*self._game_card(s))

    def _list_card(self, kind: str, adm: str, limit: int, today: date):
        found = self.snap.open_lots(kind, adm or None, today)
        if not found:
            return None, None
        rows = found[:limit]
        return (f"list:{kind}:{adm}:{limit}:{self.snap.date}",
                lambda: bot_media.list_card(kind, adm, rows, len(found), self.snap.date))

    def _lot_card(self, l: dict):
        return f"lot:{l['id']}:{l['price']}", lambda: bot_media.lot_card(l)

    def _game_card(self, s: dict):
        return f"game:{s['id']}", lambda: bot_media.game_card(s)

    def is_member(self, uid: int) -> bool:
        """Подписан ли на канал. Если проверить нельзя (бот не администратор канала) — пускаем."""
        if not self.gate or uid in self.admins:
            return True
        if self.member_cache.get(uid, 0) > self.clock():
            return True
        try:
            st = self.bot.call("getChatMember", {"chat_id": self.chat, "user_id": uid})
        except TelegramError as e:
            if not self.check_broken:
                self.log(f"Подписку проверить нельзя ({e}) — пускаю всех. Сделайте бота администратором "
                         f"канала {self.chat}.")
                self.check_broken = True
            return True
        self.check_broken = False
        ok = st.get("status") in MEMBER_STATUSES or bool(st.get("is_member"))
        self.store.set_member(uid, ok)
        if ok:
            self.member_cache[uid] = self.clock() + MEMBER_TTL
        return ok

    def _subscribe_kb(self, retry: str) -> list:
        return [[_btn("Подписаться", url=self.url)], [_btn("Готово, я подписался", retry)]]

    # -------- входящие

    def handle(self, upd: dict) -> None:
        if "message" in upd:
            self.on_message(upd["message"])
        elif "callback_query" in upd:
            self.on_callback(upd["callback_query"])

    def on_message(self, msg: dict) -> None:
        if (msg.get("chat") or {}).get("type") != "private" or "from" not in msg:
            return   # в группах и каналах бот молчит
        self.snap.refresh()
        u, chat_id = msg["from"], msg["chat"]["id"]
        cmd, _, arg = (msg.get("text") or "").strip().partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        source = (arg.strip()[:64] or None) if cmd == "/start" else None
        self.store.touch(u, self.now(), source)
        if source and source.startswith("dom_") and self.snap.find_house(source[4:]):   # чат дома, «Целый дом»
            return self.show_house(chat_id, u["id"], self.snap.find_house(source[4:]))
        if source and source[:4] in ("lot_", "dom_"):   # пост канала
            return self.show_lot(chat_id, u["id"], source[4:])
        if source == "game" or cmd == "/game":
            return self.game(chat_id, u["id"])
        if cmd in ("/parking", "/flats"):
            return self.show(chat_id, u["id"], "parking" if cmd == "/parking" else "flat", "")
        if cmd == "/alerts":
            return self.alerts(chat_id, u["id"])
        if cmd == "/channel":
            return self.send(chat_id, f"Канал «{escape(self.name)}»: лучшие лоты дня и итоги аукционов.",
                             [[_btn("Открыть канал", url=self.url)]])
        if cmd == "/stats" and u["id"] in self.admins:
            return self.send(chat_id, self.stats_text())
        self.welcome(chat_id)

    def on_callback(self, cq: dict) -> None:
        """callback_data: parking[:ЦАО] · flat[:ЦАО] · lot:<id> · a+/a-:<тип>:<округ> · alerts · g · g:<id>:<n> · menu."""
        if time.time() - cq.get("message", {}).get("date", time.time()) > 8:
            return  # протухший callback: Telegram всё равно вернёт ошибку
        self.snap.refresh()
        u, data = cq["from"], cq.get("data") or ""
        chat_id = (cq.get("message") or {}).get("chat", {}).get("id", u["id"])
        self.store.touch(u, self.now())
        self.bot.call("answerCallbackQuery", {"callback_query_id": cq["id"]})
        head, *rest = data.split(":")
        if head in lots.KINDS:
            self.show(chat_id, u["id"], head, rest[0] if rest else "")
        elif head == "lot" and rest:
            self.show_lot(chat_id, u["id"], rest[0])
        elif head in ("a+", "a-") and len(rest) == 2 and rest[0] in lots.KINDS:
            self.toggle_alert(chat_id, u["id"], head == "a+", *rest)
        elif head == "alerts":
            self.alerts(chat_id, u["id"])
        elif head == "g" and len(rest) == 2:
            self.game_answer(chat_id, u["id"], rest[0], rest[1])
        elif head == "g":
            self.game(chat_id, u["id"])
        else:
            self.welcome(chat_id)

    # -------- экраны

    def welcome(self, chat_id) -> None:
        n = {k: len(self.snap.open_lots(k, None, self._today())) for k in lots.KINDS}
        have = f"\n\nСейчас в подборке: машино-мест — {n['parking']}, квартир — {n['flat']}." if any(n.values()) else ""
        self.reply(chat_id,
                  "Город продаёт на торгах машино-места и квартиры, и часть лотов заметно дешевле соседних. "
                  "Здесь такие лоты: где, почём, на сколько ниже медианы района и до какого числа принимают заявки. "
                  "Можно включить уведомления о новых — по округу — или проверить чутьё в «Угадай цену» "
                  f"на настоящих итогах аукционов.{have}\n\n"
                  f"Лучшее за день и итоги торгов — в канале «{escape(self.name)}».",
                  [[_btn("Машино-места", "parking"), _btn("Квартиры", "flat")],
                   [_btn("Угадай цену", "g"), _btn("Уведомления", "alerts")],
                   [_btn(f"Канал «{self.name}»", url=self.url)]], COVER, bot_media.cover_card)

    def show(self, chat_id, uid: int, kind: str, adm: str) -> None:
        today = self._today()
        found = self.snap.open_lots(kind, adm or None, today)
        w = templates.WORDS[kind]
        where = f" — {adm}" if adm else ""
        if not found:
            return self.send(chat_id, f"Сейчас нет {w['forms'][2]} заметно дешевле района{where}. "
                                      "Включите уведомление — бот напишет, как только появятся.",
                             [[_btn("Уведомлять о новых", f"a+:{kind}:{adm}")], [_btn("Все округа", kind)]])
        member = self.is_member(uid)
        limit = SHOW if member else FREE
        text = (f"<b>{w['forms'][1].capitalize()} дешевле района{where}</b>\n"
                f"Ниже медианы на {templates.pct(EXPORT_MIN_DISCOUNT)} % и больше, по адресу — один лот. "
                f"Всего: {len(found)}.\n\n"
                + "\n\n".join(f"{i}. {lot_line(l)}" for i, l in enumerate(found[:limit], 1)))
        if not member:
            text += (f"\n\nОстальные лоты, выбор округа и уведомления — для подписчиков канала "
                     f"«{escape(self.name)}». Подпишитесь и нажмите «Готово».")
            return self.reply(chat_id, text, self._subscribe_kb(f"{kind}:{adm}"),
                              *self._list_card(kind, adm, limit, today))
        adms = self.snap.adms(kind, today)
        kb = [[_btn(("· " if a == adm else "") + a, f"{kind}:{a}") for a in adms[i:i + 4]]
              for i in range(0, len(adms), 4)]
        kb.append([_btn("Уведомлять о новых" + (f" в {adm}" if adm else ""), f"a+:{kind}:{adm}")])
        kb.append([_btn("Все округа", kind), _btn("Угадай цену", "g"), _btn("Канал", url=self.url)])
        self.reply(chat_id, text, kb, *self._list_card(kind, adm, limit, today))

    def show_lot(self, chat_id, uid: int, lot_id: str) -> None:
        """Карточка одного лота открыта всем: это первая польза для пришедшего по ссылке. Подборки и
        уведомления — подписчикам канала, поэтому не подписанному сразу предлагаем канал."""
        l = self.snap.find(lot_id)
        if not l:
            return self.send(chat_id, "Этого лота уже нет в подборке: заявки закрыты или цена изменилась. "
                                      "Вот что есть сейчас:",
                             [[_btn("Машино-места", "parking"), _btn("Квартиры", "flat")],
                              [_btn(f"Канал «{self.name}»", url=self.url)]])
        adm = l.get("adm") or ""
        kb = [[_btn("Похожие: " + _alert_name(l["kind"], adm), f"{l['kind']}:{adm}")],
              [_btn("Уведомлять о таких", f"a+:{l['kind']}:{adm}")]]
        text = lot_text(l)
        if not self.is_member(uid):
            text += (f"\n\nПохожие лоты, выбор округа и уведомления — для подписчиков канала «{escape(self.name)}», "
                     "там каждый день лучшие лоты и итоги торгов.")
            kb.insert(0, [_btn("Подписаться на канал", url=self.url)])
        self.reply(chat_id, text, kb, *self._lot_card(l))

    def show_house(self, chat_id, uid: int, h: dict) -> None:
        """Пачка лотов в одном доме — открыта всем, как и карточка лота."""
        w = templates.WORDS[h["kind"]]
        prices = f"от {templates.money(h['price_min'])} до {templates.money(h['price_max'])}" \
            if h["price_max"] > h["price_min"] else templates.money(h["price_min"])
        area = templates.area_range(h.get("space_min"), h.get("space_max"))
        lines = [f"<b>{h['n']} {templates.plural(h['n'], w['forms'])} в одном доме</b>", "", _where(h),
                 f"Цены: <b>{prices}</b>" + (f" · {area}" if area else "")]
        if h.get("ref"):
            lines.append(f"Медиана {h.get('scope') or 'района'}: {templates.money(h['ref'])}")
        if h.get("end_d"):
            lines.append(f"Заявки до {templates.day(h['end_d'])}"
                         + (f", торги {templates.day(h['trades_d'])}" if h.get("trades_d") else ""))
        if h.get("url"):
            lines += ["", templates.link(h["url"], "Самый дешёвый лот на torgi.mos.ru")]
        adm = h.get("adm") or ""
        kb = [[_btn("Уведомлять о новых" + (f" в {adm}" if adm else ""), f"a+:{h['kind']}:{adm}")],
              [_btn("Выгодные лоты: " + _alert_name(h["kind"], adm), f"{h['kind']}:{adm}")]]
        text = "\n".join(lines)
        if not self.is_member(uid):
            text += (f"\n\nВыгодные лоты по округам и уведомления — для подписчиков канала «{escape(self.name)}», "
                     "там каждый день лучшие лоты и итоги торгов.")
            kb.insert(0, [_btn("Подписаться на канал", url=self.url)])
        self.reply(chat_id, text, kb, f"house:{h['id']}:{h['n']}:{h['price_min']}",
                   lambda: bot_media.house_card(h))

    # -------- уведомления

    def alerts(self, chat_id, uid: int) -> None:
        have = self.store.alerts(uid)
        if have:
            text = ("<b>Ваши уведомления</b>\nБот пишет, когда появляются новые лоты дешевле района:\n"
                    + "\n".join(f"— {_alert_name(k, a)}" for k, a in have))
            kb = [[_btn(f"Отключить: {_alert_name(k, a)}", f"a-:{k}:{a}")] for k, a in have]
        else:
            text = ("Уведомлений пока нет. Выберите, о чём писать: бот пришлёт новые лоты дешевле района, "
                    "как только они появятся на торгах. Округ можно выбрать в подборке.")
            kb = []
        kb.append([_btn("+ Машино-места, вся Москва", "a+:parking:"), _btn("+ Квартиры, вся Москва", "a+:flat:")])
        self.send(chat_id, text, kb)

    def toggle_alert(self, chat_id, uid: int, on: bool, kind: str, adm: str) -> None:
        if adm and adm not in ADM.values():
            return self.welcome(chat_id)
        if not on:
            self.store.del_alert(uid, kind, adm)
            return self.alerts(chat_id, uid)
        if not self.is_member(uid):
            return self.send(chat_id, f"Уведомления — для подписчиков канала «{escape(self.name)}». "
                                      "Подпишитесь и нажмите «Готово».", self._subscribe_kb(f"a+:{kind}:{adm}"))
        self.store.add_alert(uid, kind, adm)
        self.send(chat_id, f"Готово: напишу, как только появятся новые лоты дешевле района ({_alert_name(kind, adm)}). "
                           "Список и отключение — /alerts.", [[_btn("Мои уведомления", "alerts")]])

    def notify(self, sleep=time.sleep) -> int:
        """После обновления снимка: новые лоты — тем, кто подписан на их тип и округ. Сколько сообщений ушло."""
        fresh = self.store.unseen([l["id"] for l in self.snap.lots])
        if not fresh:
            return 0
        new = [l for l in self.snap.open_lots(None, None, self._today()) if l["id"] in fresh]
        by_user: dict[int, list[dict]] = {}
        for uid, kind, adm in self.store.all_alerts():
            for l in new:
                if l["kind"] == kind and (not adm or l.get("adm") == adm) and l not in by_user.get(uid, []):
                    by_user.setdefault(uid, []).append(l)
        sent = 0
        for uid, ls in by_user.items():
            ls.sort(key=lambda l: -l["discount"])
            more = f"\n\nИ ещё {len(ls) - NOTIFY_MAX} — в подборке." if len(ls) > NOTIFY_MAX else ""
            text = (f"<b>Новые лоты дешевле района: {len(ls)}</b>\n\n"
                    + "\n\n".join(f"{i}. {lot_line(l)}" for i, l in enumerate(ls[:NOTIFY_MAX], 1)) + more)
            try:
                self.send(uid, text, [[_btn("Подборка", ls[0]["kind"]), _btn("Мои уведомления", "alerts")]])
                sent += 1
            except TelegramError as e:
                if "blocked" in str(e) or "deactivated" in str(e) or "chat not found" in str(e):
                    self.store.blocked(uid)
                else:
                    self.log(f"уведомление {uid}: {e}")
            sleep(0.05)   # лимит Telegram — около 30 сообщений в секунду
        self.log(f"Снимок {self.snap.date}: новых лотов {len(new)}, уведомлений {sent}")
        return sent

    def tick(self) -> None:
        """Между опросами: перечитать снимок и, если он новый, разослать уведомления.

        Снимок могли перечитать и при обработке сообщения, поэтому сравниваем со своей отметкой."""
        self.snap.refresh()
        if self.snap.mtime is not None and self.snap.mtime != self.notified:
            self.notified = self.snap.mtime
            self.notify()
            self.prewarm()

    # -------- игра

    def game(self, chat_id, uid: int) -> None:
        pool = [s for s in self.snap.sold if s.get("start") and s.get("final")]
        if not pool:
            return self.send(chat_id, "Итогов для игры пока нет — загляните завтра.", [[_btn("Меню", "menu")]])
        last = self.store.score(uid)["last"]
        pool = [p for p in pool if p["id"] != last] or pool
        drawn = [p for p in pool if self.cards and self.cards.ready(self._game_card(p)[0])]
        s = self.rnd.choice(drawn if len(drawn) >= 5 else pool)   # с картинкой, если их уже набралось
        w = templates.WORDS[s["kind"]]
        opts, _ = game_options(s)
        text = (f"<b>Угадай цену</b>\n\n{w['one']}"
                + (f", {templates.area(s['space'])}" if s.get("space") else "") + f"\n{_where(s)}\n"
                f"Торги {templates.day(s['date'])}. Стартовая цена — <b>{templates.money(s['start'])}</b>.\n\n"
                "За сколько продали?")
        self.reply(chat_id, text, [[_btn(templates.money(p), f"g:{s['id']}:{i}") for i, p in enumerate(opts)]],
                   *self._game_card(s))

    def game_answer(self, chat_id, uid: int, sid: str, choice: str) -> None:
        s = self.snap.find_sold(sid)
        if not s or not choice.isdigit():
            return self.game(chat_id, uid)
        _, right = game_options(s)
        ok = int(choice) == right
        sc = self.store.answer(uid, sid, ok)
        if sc is None:   # уже отвечал на этот вопрос — просто следующий
            return self.game(chat_id, uid)
        ratio = s["final"] / s["start"]
        how = ("ушло по стартовой цене — на торги вышел один участник или никто не стал повышать"
               if ratio < 1.005 else f"цена выросла в {templates.num(ratio)} раза за время торгов")
        head = "Верно!" if ok else "Мимо."
        streak = f" Серия: {sc['streak']}." if sc["streak"] > 1 else ""
        text = (f"<b>{head}</b> Продано за <b>{templates.money(s['final'])}</b>: {how}.\n\n"
                f"Счёт: {sc['won']} из {sc['played']}.{streak} Лучшая серия: {sc['best']}."
                + (f"\n{templates.link(s['url'], 'Лот на torgi.mos.ru')}" if s.get("url") else ""))
        kb = [[_btn("Ещё", "g"), _btn("Лоты сейчас", s["kind"])]]
        if sc["played"] % 3 == 0:   # не на каждом шаге: игра — повод, а не ловушка
            text += f"\n\nТакие итоги каждую неделю разбираем в канале «{escape(self.name)}»."
            kb.append([_btn(f"Канал «{self.name}»", url=self.url)])
        self.send(chat_id, text, kb)

    # -------- служебное

    def stats_text(self) -> str:
        s = self.store.stats(self.now())
        lines = [f"<b>Пользователи бота</b>: {s['total']} (остановили бота: {s['blocked']})",
                 f"новых за 7 дней: {s['week_new']}, активных: {s['week_active']}",
                 f"подписаны на канал (по последней проверке): {s['members']}",
                 f"с уведомлениями: {s['alert_users']}", f"играли: {s['players']}, раундов: {s['rounds']}",
                 f"снимок: {self.snap.date or 'нет'}, лотов {len(self.snap.lots)}, итогов {len(self.snap.sold)}",
                 "картинки: " + (f"нарисовано {len(list(self.cards.dir.glob('*.png'))) if self.cards.dir.exists() else 0}, в очереди {len(self.cards.pending)}"
                                 if self.cards and self.cards.on else "выключены (нет Chromium или Pillow)"),
                 "", "<b>Откуда пришли</b> (метка /start):"]
        lines += [f"{escape(src)}: {n} (подписаны {m or 0})" for src, n, m in s["sources"]]
        return "\n".join(lines)

    def _today(self) -> date:
        return self.now().date()


# ---------------------------------------------------------------- цикл опроса

def run(gate: Gate, bot, stop=lambda: False, sleep=time.sleep) -> None:
    """Long polling: getUpdates ждёт до 50 с; сбой сети — пауза и снова. Входящие порты не нужны."""
    offset = None
    gate.setup()
    gate.tick()   # первый запуск только запоминает лоты; после простоя — разошлёт пропущенное
    gate.log("Бот запущен, жду сообщений")
    while not stop():
        try:
            updates = bot.call("getUpdates", {"timeout": 50, "offset": offset,
                                              "allowed_updates": json.dumps(["message", "callback_query"])})
        except Exception as e:   # сеть, 5xx, конфликт с другим экземпляром — переждать
            gate.log(f"getUpdates: {e}")
            sleep(5)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                gate.handle(upd)
            except Exception as e:   # одно сообщение не должно ронять бота
                gate.log(f"update {upd.get('update_id')}: {e}")
        try:
            gate.tick()
        except Exception as e:
            gate.log(f"уведомления: {e}")


def from_env(env=os.environ) -> tuple[Gate, Bot]:
    from ..timeutil import now_msk
    from .commands import DEFAULT_CHANNEL_URL
    bot = Bot(env.get("PARKAN_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN", ""))
    url = env.get("PARKAN_CHANNEL_URL", DEFAULT_CHANNEL_URL).strip() or DEFAULT_CHANNEL_URL
    state = Path(env.get("PARKAN_CHANNEL_STATE", "channel_state"))
    admins = [a for a in re.split(r"[,\s]+", env.get("PARKAN_BOT_ADMINS", "")) if a.isdigit()]
    db = env.get("PARKAN_BOT_DB") or "bot.sqlite"
    log = lambda msg: print(msg, file=sys.stderr, flush=True)   # noqa: E731
    cards = None if env.get("PARKAN_BOT_CARDS") == "0" else \
        bot_media.Cards(env.get("PARKAN_BOT_CARDS") or Path(db).parent / "cards", log=log)
    gate = Gate(bot, Snapshot(env.get("PARKAN_BOT_LOTS") or state / "bot" / "lots.json"), Store(db), url,
                chat=env.get("PARKAN_CHANNEL_CHAT") or None, admins=admins,
                gate=env.get("PARKAN_BOT_GATE", "1") != "0", now=now_msk, log=log, cards=cards)
    log("Картинки: " + ("рисую в фоне, папка " + str(cards.dir) if cards and cards.on
                        else "выключены — нет Chromium или Pillow (pip install -e '.[channel]', apt install chromium)"))
    return gate, bot
