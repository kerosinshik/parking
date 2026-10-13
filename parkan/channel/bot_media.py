"""Картинки бота-дорвея: те же карточки, что в канале (html_cards), отрисовка в фоне.

Холодный старт Chromium — секунды, а бот в чате должен отвечать сразу. Поэтому бот никогда не ждёт картинку:
есть готовая — отправляет фото с подписью, нет — отвечает текстом и ставит карточку в очередь. После обновления
снимка очередь заранее заполняется самым ходовым: обложка, подборки по округам, лучшие лоты, вопросы игры.
Отправленная картинка запоминается по file_id Telegram — повторно файл не загружается.

Без Chromium или Pillow картинок нет, бот работает текстом, как раньше.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import queue
import threading
import time
from pathlib import Path

from . import templates

KEEP_DAYS = 3   # отрисованные карточки старше — удаляются при обновлении снимка


class Cards:
    def __init__(self, folder, render=None, available=None, sync: bool = False, log=print):
        """render(card, path) и available() — подмена html_cards в тестах; sync — рисовать сразу, без потока."""
        from . import html_cards
        self.dir = Path(folder)
        self.render = render or html_cards.render
        on = available() if available else html_cards.available()
        if on and render is None:
            try:
                import PIL  # noqa: F401  — html_cards сохраняет PNG через Pillow
            except ImportError:
                on = False
        self.on, self.sync, self.log = on, sync, log
        self.file_ids: dict[str, str] = {}
        self.pending: set[str] = set()
        self.lock = threading.Lock()
        self.q: queue.PriorityQueue = queue.PriorityQueue()
        self.seq = itertools.count()
        self.thread = None

    def path(self, key: str) -> Path:
        return self.dir / (hashlib.sha1(key.encode()).hexdigest()[:20] + ".png")

    def ready(self, key: str) -> Path | None:
        p = self.path(key)
        return p if self.on and p.exists() else None

    def want(self, key: str, make, urgent: bool = False) -> None:
        """Поставить в очередь; make() — словарь карточки, считается только при отрисовке.

        urgent — карточку ждёт человек: она рисуется раньше фонового прогрева."""
        if not self.on or self.ready(key):
            return
        with self.lock:
            if key in self.pending and not urgent:
                return
            self.pending.add(key)
        if self.sync:
            self._draw(key, make)
            return
        self.q.put((0 if urgent else 1, next(self.seq), key, make))
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._work, name="cards", daemon=True)
            self.thread.start()

    def get(self, key: str, make) -> Path | None:
        p = self.ready(key)
        if p is None:
            self.want(key, make, urgent=True)
            p = self.ready(key)   # в синхронном режиме уже готова
        return p

    def _draw(self, key: str, make) -> None:
        out = self.path(key)
        if out.exists():   # уже нарисована: срочный дубль из очереди
            return
        tmp = out.with_suffix(".tmp.png")
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.render(make(), tmp)
            os.replace(tmp, out)   # бот не отправит недорисованный файл
        except Exception as e:   # одна карточка не должна останавливать очередь
            self.log(f"карточка {key}: {e}")
            tmp.unlink(missing_ok=True)
        finally:
            with self.lock:
                self.pending.discard(key)

    def _work(self) -> None:
        while True:
            _, _, key, make = self.q.get()
            self._draw(key, make)

    def cleanup(self, keep_days: int = KEEP_DAYS) -> None:
        if not self.on:
            return
        old = time.time() - keep_days * 86400
        for p in self.dir.glob("*.png") if self.dir.exists() else ():
            if p.stat().st_mtime < old:
                p.unlink(missing_ok=True)


# ---------------------------------------------------------------- карточки бота (макеты канала)

def cover_card() -> dict:
    return templates._card("cover", "БОТ КАНАЛА", type="parking", title="Торги Москвы дешевле рынка",
                           lede="Машино-места и квартиры от города: где, почём и до какого числа принимают заявки")


def list_card(kind: str, adm: str, lots: list[dict], total: int, day: str | None) -> dict:
    w = templates.WORDS[kind]
    return templates._card(
        "list", "ПОДБОРКА" + (f" · {adm}" if adm else ""), headline=str(total),
        headline_sub=f"{templates.plural(total, w['forms'])} дешевле района — заявки ещё принимают",
        deadline=f"на {templates.day(day)}" if day else "", districts=[l.get("district") for l in lots],
        rows=[{"lead": f"−{templates.pct(l['discount'])}%", "price": templates.money(l["price"]),
               "place": templates.district(l.get("district")),
               "until": f"до {templates.short_day(l['end_d'])}" if l.get("end_d") else ""} for l in lots])


def lot_card(l: dict) -> dict:
    return dict(templates.lot_card(l), kicker="ЛОТ НА ТОРГАХ")


def game_card(s: dict) -> dict:
    """Вопрос игры: старт и место видно, итог — нет."""
    return templates._card("lot", "УГАДАЙ ЦЕНУ", type=s["kind"], headline="?", headline_sub="за сколько продали?",
                           deadline=f"торги {templates.day(s['date'])}" if s.get("date") else "",
                           price=templates.money(s["start"]),
                           price_sub=" · ".join(x for x in ("стартовая цена", templates.area(s.get("space"))) if x),
                           address=s["place"], district=s.get("district"))
