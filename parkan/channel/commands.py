"""Команды CLI канала: channel-draft, channel-send, channel-lint, channel-check, channel-export, channel-bot, channel-market, market-add, market-status, market-rosstat, market-page."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path

from ..timeutil import now_msk
from ..track import digest
from ..track.commands import default_state_dir
from . import content, copy, endings, html_cards, lots, market, market_check, templates
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
                           registry=digest.load_registry(registry_path), market_path=a.market)
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
                         "copy": f"{stem}.txt", "card": card.name if card else None, "sent": False,
                         "market_lot": p.market_lot})
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
        lot = m.get("market_lot")
        try:
            msg = bot.send_post(chat, text, day / m["card"] if m.get("card") else None)
        except Exception as e:   # один черновик не должен срывать остальные
            print(f"  {m['key']}: {e}", file=sys.stderr)
            failed += 1
            continue
        markup = market_check.reply_markup(lot) if lot else None
        if markup and (msg or {}).get("message_id"):
            # кнопки — отдельным сообщением-ответом: черновик пересылается в канал без них;
            # ответ автора с объявлениями найдём по id любого из двух сообщений
            ids = [msg["message_id"]]
            try:
                btn = bot.send_buttons(chat, msg["message_id"], market_check.BUTTONS_TEXT, markup)
                if (btn or {}).get("message_id"):
                    ids.append(btn["message_id"])
            except Exception as e:
                print(f"  {m['key']}: кнопки не ушли: {e}", file=sys.stderr)
            for mid in ids:
                market_check.remember(registry_path.parent, mid, m["key"], m["rubric"], lot, today)
        m["sent"] = True
        sent.append(m["key"])
        if m.get("same"):   # «Рядом на рынке»: тот же лот с теми же объявлениями больше не придёт
            sent.append(m["same"])
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


def cmd_channel_split_buttons(a, con) -> int:
    """Черновики, отправленные с кнопками под самим сообщением (до 14.10.2026): снять кнопки и прислать их
    отдельным ответом, чтобы черновик пересылался в канал без них. Обработанные помечаются в kitchen.json."""
    chat = a.chat or os.environ.get("TELEGRAM_DRAFT_CHAT")
    if not chat:
        print("Не задан чат кухни: --chat или TELEGRAM_DRAFT_CHAT", file=sys.stderr)
        return 1
    bot = Bot(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    path = Path(a.channel_state) / market_check.KITCHEN
    kitchen = market_check._load(path, {})
    since = a.since or "0000-00-00"
    todo = [(mid, e) for mid, e in kitchen.items() if e.get("sent", "") >= since and not e.get("split")]
    done, failed = 0, 0
    for mid, e in todo:
        markup = market_check.reply_markup(e["lot"])
        try:
            bot.call("editMessageReplyMarkup", {"chat_id": chat, "message_id": mid,
                                                "reply_markup": json.dumps({"inline_keyboard": []})})
            btn = bot.send_buttons(chat, int(mid), market_check.BUTTONS_TEXT, markup) if markup else None
        except Exception as ex:
            print(f"  {mid}: {ex}", file=sys.stderr)
            failed += 1
            continue
        e["split"] = True
        if (btn or {}).get("message_id"):
            kitchen[str(btn["message_id"])] = dict(e, split=True)
        done += 1
    path.write_text(json.dumps(kitchen, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Кнопки вынесены в отдельные сообщения: {done}, ошибок: {failed}")
    return 1 if failed else 0


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


def cmd_channel_export(a, con) -> int:
    """Снимок для бота-дорвея: лоты в продаже дешевле медианы и итоги торгов для игры."""
    from . import bot
    today = date.fromisoformat(a.today) if a.today else now_msk().date()
    out = a.out or Path(a.channel_state) / "bot" / "lots.json"
    data = bot.export(con, a.state_dir, today, out, results_path=a.results)
    print(f"Снимок для бота: лотов {len(data['lots'])}, итогов для игры {len(data['sold'])} → {out}")
    return 0


def cmd_channel_bot(a, con) -> int:
    """Бот-дорвей: long polling, работает постоянно (systemd на VPS, deploy/)."""
    from . import bot
    gate, tg = bot.from_env()
    if a.setup_only:
        gate.setup()
        print("Описание и команды бота обновлены")
        return 0
    bot.run(gate, tg)
    return 0


def cmd_channel_market(a, con) -> int:
    """Ответы автора в кухне с объявлениями рядом с лотом → черновики «Рядом на рынке» в папке дня (market_check).

    Дальше как у channel-draft: текст по навыку channel-copy, channel-lint, channel-send. Ошибки в строках бот
    сразу называет ответом в кухне."""
    today = date.fromisoformat(a.today) if a.today else now_msk().date()
    chat = a.chat or os.environ.get("TELEGRAM_DRAFT_CHAT")
    if not chat:
        print("Не задан чат кухни: --chat или TELEGRAM_DRAFT_CHAT", file=sys.stderr)
        return 1
    bot = Bot(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    found, problems = market_check.collect(bot, a.channel_state, chat, today)
    for reply_id, errors in problems:
        try:
            bot.reply(chat, reply_id, "Не всё разобрал:\n" + "\n".join(errors) +
                      "\nФормат — по строке на объявление: ссылка, цена, площадь. Например: "
                      "https://www.cian.ru/sale/flat/123/ 14,5 млн 42")
        except Exception as e:
            print(f"  ответ в кухню не ушёл: {e}", file=sys.stderr)
    out = Path(a.out) / today.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "drafts.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
    registry = digest.load_registry(Path(a.channel_state) / "drafted.json")
    draw = html_cards.available()
    made = 0
    for f in found:
        key = f"market:{f['entry']['key']}:{f['reply_id']}"
        # тот же лот с теми же объявлениями (автор ответил на два черновика одного адреса) — один пост
        same = "market-same:" + hashlib.sha1(json.dumps(
            [f["entry"]["lot"].get("address"), sorted(i["url"] for i in f["items"])], ensure_ascii=False
        ).encode()).hexdigest()[:12]
        if key in registry or same in registry or any(m["key"] in (key, same) or m.get("same") == same
                                                         for m in manifest):
            continue
        lot, items = f["entry"]["lot"], f["items"]
        c = market_check.compare(lot, items)
        stem = f"{len(manifest) + 1:02d}-market"
        (out / f"{stem}.html").write_text(market_check.text(lot, items, c, today) + "\n", encoding="utf-8")
        (out / f"{stem}.facts.json").write_text(json.dumps(market_check.facts(lot, items, c, today),
                                                           ensure_ascii=False, indent=1), encoding="utf-8")
        card = None
        if draw:
            try:
                card = html_cards.render(market_check.card(lot, c), out / f"{stem}.png")
            except Exception as e:
                print(f"  {key}: карточка не нарисовалась: {e}", file=sys.stderr)
        manifest.append({"key": key, "rubric": "market", "facts": f"{stem}.facts.json", "template": f"{stem}.html",
                         "copy": f"{stem}.txt", "card": card.name if card else None, "sent": False, "same": same})
        made += 1
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    endings.write_list(out / "endings.txt", endings.load(Path(a.channel_state) / "endings.json"))
    print(f"Ответов с объявлениями: {len(found)}, новых черновиков «Рядом на рынке»: {made}, "
          f"ответов с ошибками: {len(problems)} → {out}")
    return 0


def cmd_market_add(a, con) -> int:
    """Дописывает наблюдения рыночных цен из CSV в журнал (market.py): проверка, без повторов, только добавление."""
    today = date.fromisoformat(a.today) if a.today else now_msk().date()
    rows = market.read_rows(a.file)
    added, dup, errors = market.add(a.market, rows, today)
    for e in errors:
        print(e, file=sys.stderr)
    if errors:
        print(f"Ничего не добавлено: ошибок {len(errors)}. Колонки: {', '.join(market.FIELDS[:-1])}", file=sys.stderr)
        return 1
    print(f"Добавлено наблюдений: {added}, повторов пропущено: {dup} → {a.market}")
    return 0


def cmd_market_status(a, con) -> int:
    """Что есть в журнале рынка и по каким районам с квартирами в продаже не хватает свежего ориентира."""
    today = date.fromisoformat(a.today) if a.today else now_msk().date()
    obs = market.load(a.market)
    if not obs:
        print(f"Журнал рынка пуст: {a.market}. Добавить — parkan market-add --file <csv>")
    else:
        print(f"Наблюдений: {len(obs)}, источники: {', '.join(sorted({o['source'] for o in obs}))}")
        for m, (what, _, _) in market.METRICS.items():
            ds = [o["observed"] for o in obs if o["metric"] == m and o["observed"]]
            print(f"  {what}: {len(ds)} строк" + (f", самое свежее — {max(ds)}" if ds else ""))
    if not lots.load(con, a.state_dir, today):
        return 0
    districts = con.execute("SELECT DISTINCT district, adm FROM ch_sale WHERE kind = 'flat' AND district IS NOT NULL "
                            "ORDER BY district").fetchall()
    age = content.POLICY["market_max_age_days"]
    cov = market.coverage(obs, districts, today, age)
    print(f"Районов с квартирами в продаже: {len(districts)}; свежий (≤ {age['район']} дн.) ориентир по району — "
          f"{len(cov['район'])}, по округу — {len(cov['округ'])}, по Москве — {len(cov['город'])}, нет — {len(cov['нет'])}")
    need = cov["нет"] + cov["округ"] + cov["город"]
    if need:
        print("Собрать по районам: " + ", ".join(templates.district(d) for d in need))
    return 0


def cmd_market_rosstat(a, con) -> int:
    """Средние цены на рынке жилья Москвы от Росстата (квартальные XLSX) → журнал рынка."""
    from . import rosstat
    today = date.fromisoformat(a.today) if a.today else now_msk().date()
    rows = rosstat.collect()
    added, dup, errors = market.add(a.market, rows, today)
    for e in errors:
        print(e, file=sys.stderr)
    if errors:
        return 1
    last = max((r["observed"] for r in rows), default=None)
    print(f"Росстат: строк {len(rows)} (последний квартал — на {last}), добавлено {added}, уже были {dup} → {a.market}")
    return 0


def cmd_market_page(a, con) -> int:
    """Отдельная страница «Рынок квартир»: журнал рынка, квартиры в продаже и итоги торгов на его фоне."""
    from .market_page import render
    today = date.fromisoformat(a.today) if a.today else now_msk().date()
    out = render(con, a.state_dir, a.out, today=today, market_path=a.market, results_path=a.results)
    print(f"Страница рынка: {out}")
    return 0


def register(sub) -> None:
    sp = sub.add_parser("channel-draft", help="черновики постов для Telegram-канала (docs/channel.md)")
    sp.add_argument("--state-dir", default=default_state_dir(), help="журнал Пульса (track-sync)")
    sp.add_argument("--results", help="таблицы итогов торгов (track-torgi --results), несколько — через запятую")
    sp.add_argument("--out", default="channel_out", help="куда сложить тексты и карточки (подпапка на дату)")
    sp.add_argument("--channel-state", default=os.environ.get("PARKAN_CHANNEL_STATE", "channel_state"),
                    help="состояние канала: что уже уходило в черновики")
    sp.add_argument("--rubrics", help="lot,drop,house,sold,nofight,torgi,deadline,snapshot,week,weekstats,analytics (по умолчанию: "
                         "snapshot — по вторникам и четвергам, week — по четвергам, weekstats — по пятницам, "
                         "analytics — 1-го числа, остальное — каждый день)")
    sp.add_argument("--send", action="store_true", help="сразу отправить в кухню (без написанных текстов — шаблонные)")
    sp.add_argument("--chat", help="чат для черновиков (иначе TELEGRAM_DRAFT_CHAT)")
    sp.add_argument("--today", help="дата ГГГГ-ММ-ДД вместо сегодняшней (для проверки)")
    sp.add_argument("--market", default=os.environ.get("PARKAN_MARKET", market.DEFAULT_PATH),
                    help="журнал рыночных цен (market-add); нет файла — без сравнения с рынком")
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

    sp = sub.add_parser("channel-split-buttons",
                        help="снять кнопки с отправленных черновиков и прислать их отдельным ответом")
    sp.add_argument("--channel-state", default=os.environ.get("PARKAN_CHANNEL_STATE", "channel_state"))
    sp.add_argument("--chat", help="чат кухни (иначе TELEGRAM_DRAFT_CHAT)")
    sp.add_argument("--since", help="только черновики, отправленные с этой даты (ГГГГ-ММ-ДД)")
    sp.set_defaults(fn=cmd_channel_split_buttons, track=True)

    sp = sub.add_parser("channel-check", help="проверить бота и доступ к кухне")
    sp.add_argument("--chat", help="чат для черновиков (иначе TELEGRAM_DRAFT_CHAT)")
    sp.add_argument("--ping", action="store_true", help="отправить в кухню тестовое сообщение")
    sp.set_defaults(fn=cmd_channel_check, track=True)

    sp = sub.add_parser("channel-export", help="снимок лотов и итогов для бота-дорвея (docs/channel-bot.md)")
    sp.add_argument("--state-dir", default=default_state_dir(), help="журнал Пульса (track-sync)")
    sp.add_argument("--results", help="таблицы итогов торгов для игры «Угадай цену», через запятую")
    sp.add_argument("--channel-state", default=os.environ.get("PARKAN_CHANNEL_STATE", "channel_state"))
    sp.add_argument("--out", help="файл снимка (по умолчанию <channel-state>/bot/lots.json)")
    sp.add_argument("--today", help="дата ГГГГ-ММ-ДД вместо сегодняшней")
    sp.set_defaults(fn=cmd_channel_export, track=True)

    sp = sub.add_parser("channel-bot", help="запустить бота-дорвея (переменные — docs/channel-bot.md)")
    sp.add_argument("--setup-only", action="store_true", help="только обновить описание и меню команд")
    sp.set_defaults(fn=cmd_channel_bot, track=True)

    sp = sub.add_parser("channel-market", help="ответы автора с объявлениями рядом с лотом → черновики «Рядом на рынке»")
    sp.add_argument("--out", default="channel_out", help="папка черновиков (подпапка на дату, как у channel-draft)")
    sp.add_argument("--channel-state", default=os.environ.get("PARKAN_CHANNEL_STATE", "channel_state"))
    sp.add_argument("--chat", help="чат кухни (иначе TELEGRAM_DRAFT_CHAT)")
    sp.add_argument("--today", help="дата ГГГГ-ММ-ДД вместо сегодняшней")
    sp.set_defaults(fn=cmd_channel_market, track=True)

    sp = sub.add_parser("market-add", help="дописать рыночные цены из CSV в журнал рынка (docs/channel.md)")
    sp.add_argument("--file", required=True, help="CSV с колонками " + ", ".join(market.FIELDS[:-1]))
    sp.add_argument("--market", default=os.environ.get("PARKAN_MARKET", market.DEFAULT_PATH), help="журнал рынка")
    sp.add_argument("--today", help="дата ГГГГ-ММ-ДД вместо сегодняшней")
    sp.set_defaults(fn=cmd_market_add, track=True)

    sp = sub.add_parser("market-status", help="что есть в журнале рынка и каких районов не хватает")
    sp.add_argument("--market", default=os.environ.get("PARKAN_MARKET", market.DEFAULT_PATH), help="журнал рынка")
    sp.add_argument("--state-dir", default=default_state_dir(), help="журнал Пульса (квартиры в продаже)")
    sp.add_argument("--today", help="дата ГГГГ-ММ-ДД вместо сегодняшней")
    sp.set_defaults(fn=cmd_market_status, track=True)

    sp = sub.add_parser("market-page", help="страница «Рынок квартир»: журнал рынка и квартиры на торгах на его фоне")
    sp.add_argument("--market", default=os.environ.get("PARKAN_MARKET", market.DEFAULT_PATH), help="журнал рынка")
    sp.add_argument("--state-dir", default=default_state_dir(), help="журнал Пульса (квартиры в продаже)")
    sp.add_argument("--results", help="таблицы итогов торгов квартир (track-torgi --types квартира), через запятую")
    sp.add_argument("--out", default="data/market.html")
    sp.add_argument("--today", help="дата ГГГГ-ММ-ДД вместо сегодняшней")
    sp.set_defaults(fn=cmd_market_page, track=True)

    sp = sub.add_parser("market-rosstat", help="средние цены на жильё в Москве от Росстата → журнал рынка")
    sp.add_argument("--market", default=os.environ.get("PARKAN_MARKET", market.DEFAULT_PATH), help="журнал рынка")
    sp.add_argument("--today", help="дата ГГГГ-ММ-ДД вместо сегодняшней")
    sp.set_defaults(fn=cmd_market_rosstat, track=True)
