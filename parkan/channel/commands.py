"""Команды CLI канала: channel-draft, channel-send, channel-lint, channel-check."""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

from ..timeutil import now_msk
from ..track import digest
from ..track.commands import default_state_dir
from . import content, copy, endings, html_cards
from .telegram import Bot


def cmd_channel_draft(a, con) -> int:
    """Собирает черновики дня в папку: факты (.facts.json), шаблонный текст (.html), карточку (.png).

    Текст пишет Claude по навыку .claude/skills/channel-copy в файл .txt рядом; отправляет channel-send.
    С --send отправляет сразу — чем есть (написанный текст, иначе шаблонный).
    """
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
        (out / f"{stem}.facts.json").write_text(json.dumps(p.facts or {}, ensure_ascii=False, indent=1, default=str),
                                                encoding="utf-8")
        card = None
        if p.card and draw:
            try:
                card = html_cards.render(p.card, out / f"{stem}.png")
            except Exception as e:   # без карточки черновик всё равно полезен
                print(f"  {p.key}: карточка не нарисовалась: {e}", file=sys.stderr)
        manifest.append({"key": p.key, "rubric": p.rubric, "facts": f"{stem}.facts.json", "template": f"{stem}.html",
                         "copy": f"{stem}.txt", "card": card.name if card else None, "sent": False})
    (out / "drafts.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    endings.write_list(out / "endings.txt", endings.load(Path(a.channel_state) / "endings.json"))
    print(f"Черновиков: {len(posts)} ({', '.join(p.rubric for p in posts) or 'нечего предложить'}) → {out}")
    if a.send and posts:
        return send_dir(out, registry_path, a.chat, today)
    return 0


DEFAULT_CHANNEL_URL = "https://t.me/poputny_metr"


def with_signature(text: str) -> str:
    """Подпись-ссылка на канал в конце поста (PARKAN_CHANNEL_URL): в канале она не мешает, а при пересылке
    поста в чат или другой канал ведёт читателя к нам. Ставит код, не автор текста, — одинаково везде.
    PARKAN_CHANNEL_URL="" отключает подпись."""
    url = os.environ.get("PARKAN_CHANNEL_URL", DEFAULT_CHANNEL_URL).strip()
    if not url or url in text:
        return text
    from html import escape
    from .templates import channel_name
    return f'{text}\n\n<a href="{escape(url, quote=True)}">{escape(channel_name())}</a>'


def _used_endings(state_dir) -> list[str]:
    return [u["text"] for u in endings.load(Path(state_dir) / "endings.json")] + endings.skill_examples()


def _text_for(day: Path, m: dict, used: list[str]) -> tuple[str, str]:
    """Текст поста: написанный (.txt), если прошёл проверку, иначе шаблонный. Второе — откуда он."""
    copy_path = day / m["copy"]
    if copy_path.exists():
        text = copy_path.read_text(encoding="utf-8").strip()
        problems = copy.check(text, copy.load_facts(day / m["facts"]), with_card=bool(m.get("card")),
                              used_endings=used)
        if not problems:
            return text, "написан"
        print(f"  {m['key']}: текст не прошёл проверку ({'; '.join(problems)}) — уйдёт шаблонный", file=sys.stderr)
    return (day / m["template"]).read_text(encoding="utf-8").strip(), "шаблон"


def send_dir(day: Path, registry_path: Path, chat: str | None, today: date) -> int:
    """Отправляет в кухню ещё не отправленные черновики папки дня и запоминает их."""
    chat = chat or os.environ.get("TELEGRAM_DRAFT_CHAT")
    if not chat:
        print("Не задан чат для черновиков: --chat или TELEGRAM_DRAFT_CHAT", file=sys.stderr)
        return 1
    manifest_path = day / "drafts.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bot = Bot(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    sent, failed, how, new_endings = [], 0, [], []
    used = _used_endings(registry_path.parent)
    for m in manifest:
        if m.get("sent"):
            continue
        text, source = _text_for(day, m, used + new_endings)
        text = with_signature(text)
        try:
            bot.send_post(chat, text, day / m["card"] if m.get("card") else None)
        except Exception as e:   # один черновик не должен срывать остальные
            print(f"  {m['key']}: {e}", file=sys.stderr)
            failed += 1
            continue
        m["sent"] = True
        sent.append(m["key"])
        how.append(source)
        last = endings.ending(copy.visible(text.rsplit("\n\n<a href", 1)[0])) if source == "написан" else None
        if last:
            new_endings.append(last)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    digest.mark_sent(registry_path, sent, today, keep_days=120)
    endings.record(registry_path.parent / "endings.json", new_endings, today)
    print(f"Отправлено в черновики: {len(sent)} (написанных текстов: {how.count('написан')}, "
          f"шаблонных: {how.count('шаблон')}), ошибок: {failed}")
    return 1 if failed else 0


def _day_dir(a) -> Path:
    if a.dir:
        return Path(a.dir)
    today = date.fromisoformat(a.today) if a.today else now_msk().date()
    return Path(a.out) / today.isoformat()


def cmd_channel_send(a, con) -> int:
    day = _day_dir(a)
    return send_dir(day, Path(a.channel_state) / "drafted.json", a.chat, date.fromisoformat(day.name)
                    if len(day.name) == 10 else now_msk().date())


def cmd_channel_lint(a, con) -> int:
    """Проверка написанных текстов папки дня: цифры только из фактов, разметка, длина."""
    day = _day_dir(a)
    bad = 0
    used = _used_endings(a.channel_state)
    for m in json.loads((day / "drafts.json").read_text(encoding="utf-8")):
        copy_path = day / m["copy"]
        if not copy_path.exists():
            print(f"{m['copy']}: текста нет — уйдёт шаблонный")
            continue
        text = copy_path.read_text(encoding="utf-8")
        problems = copy.check(text, copy.load_facts(day / m["facts"]), with_card=bool(m.get("card")), used_endings=used)
        last = endings.ending(copy.visible(text))
        if last:
            used = used + [last]   # концовки внутри одного дня тоже не должны повторяться
        bad += bool(problems)
        print(f"{m['copy']}: " + ("в порядке" if not problems else "; ".join(problems)))
    return 1 if bad else 0


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
    sp.add_argument("--results", help="таблицы итогов торгов (track-torgi --results), несколько — через запятую")
    sp.add_argument("--out", default="channel_out", help="куда сложить тексты и карточки (подпапка на дату)")
    sp.add_argument("--channel-state", default=os.environ.get("PARKAN_CHANNEL_STATE", "channel_state"),
                    help="состояние канала: что уже уходило в черновики")
    sp.add_argument("--rubrics", help="lot,drop,house,sold,nofight,week,weekstats (по умолчанию всё, кроме week — по понедельникам "
                         "и weekstats — по пятницам)")
    sp.add_argument("--send", action="store_true", help="сразу отправить в кухню (без написанных текстов — шаблонные)")
    sp.add_argument("--chat", help="чат для черновиков (иначе TELEGRAM_DRAFT_CHAT)")
    sp.add_argument("--today", help="дата ГГГГ-ММ-ДД вместо сегодняшней (для проверки)")
    sp.set_defaults(fn=cmd_channel_draft, track=True)

    for name, fn, hlp in (("channel-send", cmd_channel_send, "отправить черновики папки дня в кухню (тексты .txt, иначе шаблон)"),
                          ("channel-lint", cmd_channel_lint, "проверить написанные тексты папки дня")):
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("--dir", help="папка дня (иначе --out/<дата>)")
        sp.add_argument("--out", default="channel_out")
        sp.add_argument("--today", help="дата ГГГГ-ММ-ДД вместо сегодняшней")
        sp.add_argument("--channel-state", default=os.environ.get("PARKAN_CHANNEL_STATE", "channel_state"))
        sp.add_argument("--chat", help="чат для черновиков (иначе TELEGRAM_DRAFT_CHAT)")
        sp.set_defaults(fn=fn, track=True)

    sp = sub.add_parser("channel-check", help="проверить бота и доступ к кухне")
    sp.add_argument("--chat", help="чат для черновиков (иначе TELEGRAM_DRAFT_CHAT)")
    sp.add_argument("--ping", action="store_true", help="отправить в кухню тестовое сообщение")
    sp.set_defaults(fn=cmd_channel_check, track=True)
