"""Команды CLI канала: channel-draft, channel-check."""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

from ..timeutil import now_msk
from ..track import digest
from ..track.commands import default_state_dir
from . import content, html_cards
from .telegram import Bot


def cmd_channel_draft(a, con) -> int:
    today = date.fromisoformat(a.today) if a.today else now_msk().date()
    registry_path = Path(a.channel_state) / "drafted.json"
    rubrics = tuple(r.strip() for r in a.rubrics.split(",")) if a.rubrics else None
    posts = content.drafts(con, a.state_dir, today, results_path=a.results, rubrics=rubrics,
                           registry=digest.load_registry(registry_path))
    out = Path(a.out) / today.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    draw = html_cards.available()
    if not draw and any(p.card for p in posts):
        print("Карточки не рисуются: нет Chromium (PARKAN_CHROME), черновики уйдут текстом", file=sys.stderr)
    manifest = []
    for i, p in enumerate(posts, 1):
        stem = f"{i:02d}-{p.rubric}"
        (out / f"{stem}.html").write_text(p.text + "\n", encoding="utf-8")
        card = None
        if p.card and draw:
            try:
                card = html_cards.render(p.card, out / f"{stem}.png")
            except Exception as e:   # без карточки черновик всё равно полезен
                print(f"  {p.key}: карточка не нарисовалась: {e}", file=sys.stderr)
        manifest.append({"key": p.key, "rubric": p.rubric, "text": f"{stem}.html", "card": card.name if card else None})
        p.card_path = card
    (out / "drafts.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Черновиков: {len(posts)} ({', '.join(p.rubric for p in posts) or 'нечего предложить'}) → {out}")

    if a.send and posts:
        chat = a.chat or os.environ.get("TELEGRAM_DRAFT_CHAT")
        if not chat:
            print("Не задан чат для черновиков: --chat или TELEGRAM_DRAFT_CHAT", file=sys.stderr)
            return 1
        bot = Bot(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
        sent = []
        for p in posts:
            try:
                bot.send_post(chat, p.text, p.card_path)
            except Exception as e:   # один черновик не должен срывать остальные
                print(f"  {p.key}: {e}", file=sys.stderr)
                continue
            sent.append(p.key)
        digest.mark_sent(registry_path, sent, today, keep_days=120)
        print(f"Отправлено в черновики: {len(sent)} из {len(posts)}")
        return 0 if len(sent) == len(posts) else 1
    return 0


def cmd_channel_check(a, con) -> int:
    """Проверка настройки: токен, доступ бота к кухне, права на публикацию."""
    chat = a.chat or os.environ.get("TELEGRAM_DRAFT_CHAT")
    bot = Bot(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    me = bot.call("getMe", {})
    print(f"Бот: @{me['username']}")
    if not chat:
        print("Не задан чат для черновиков: --chat или TELEGRAM_DRAFT_CHAT", file=sys.stderr)
        return 1
    try:
        info = bot.call("getChat", {"chat_id": chat})
    except Exception as e:
        print(f"Чат {chat} недоступен боту: {e}\n"
              "Добавьте бота в кухню администратором с правом публиковать сообщения и проверьте id "
              "(у каналов он вида -100…).", file=sys.stderr)
        return 1
    member = bot.call("getChatMember", {"chat_id": chat, "user_id": me["id"]})
    can_post = member.get("status") == "administrator" and member.get("can_post_messages", True)
    print(f"Кухня: «{info.get('title')}» ({info.get('type')}); бот — {member.get('status')}"
          f"{'' if can_post else ', публиковать не может'}")
    if not can_post:
        return 1
    if a.ping:
        bot.call("sendMessage", {"chat_id": chat, "text": "Проверка связи: бот «Попутного Метра» на месте."})
        print("Тестовое сообщение отправлено.")
    return 0


def register(sub) -> None:
    sp = sub.add_parser("channel-draft", help="черновики постов для Telegram-канала (docs/channel.md)")
    sp.add_argument("--state-dir", default=default_state_dir(), help="журнал Пульса (track-sync)")
    sp.add_argument("--results", help="таблица итогов торгов (track-torgi --results)")
    sp.add_argument("--out", default="channel_out", help="куда сложить тексты и карточки (подпапка на дату)")
    sp.add_argument("--channel-state", default=os.environ.get("PARKAN_CHANNEL_STATE", "channel_state"),
                    help="состояние канала: что уже уходило в черновики")
    sp.add_argument("--rubrics", help="lot,drop,house,sold,nofight,week,weekstats (по умолчанию всё, кроме week — по понедельникам "
                         "и weekstats — по пятницам)")
    sp.add_argument("--send", action="store_true", help="отправить черновики в Telegram и запомнить их")
    sp.add_argument("--chat", help="чат для черновиков (иначе TELEGRAM_DRAFT_CHAT)")
    sp.add_argument("--today", help="дата ГГГГ-ММ-ДД вместо сегодняшней (для проверки)")
    sp.set_defaults(fn=cmd_channel_draft, track=True)

    sp = sub.add_parser("channel-check", help="проверить бота и доступ к кухне")
    sp.add_argument("--chat", help="чат для черновиков (иначе TELEGRAM_DRAFT_CHAT)")
    sp.add_argument("--ping", action="store_true", help="отправить в кухню тестовое сообщение")
    sp.set_defaults(fn=cmd_channel_check, track=True)
