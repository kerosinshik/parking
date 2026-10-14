"""Проверка текста поста, написанного по навыку channel-copy, перед отправкой в кухню.

- разметка — только теги, которые понимает Telegram (parse_mode=HTML), и все закрыты;
- длина: с карточкой текст идёт подписью к фото, лимит Telegram — 1024 знака;
- стиль: без хэштегов и эмодзи (решение автора 12.10.2026);
- рынок: рыночная цена за м² и цена по Москве в тексте — только вместе с источником (market_source, moscow_source);
- цифры: каждое число в тексте должно быть в фактах поста. Выдуманная или пересчитанная цифра —
  главный риск текста от модели, поэтому такой текст не отправляется, вместо него уходит шаблонный.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

ALLOWED = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "blockquote",
           "tg-spoiler", "span"}
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
SMALL = 12          # небольшие целые («в 2 раза», «3 места», «1 участник») не требуют опоры в фактах

EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")
NUM = re.compile(r"\d+(?:[  ]\d{3})*(?:[.,]\d+)?")


def _norm(token: str) -> str:
    return token.replace(" ", "").replace(" ", "").replace(",", ".")


def numbers(text: str) -> set[str]:
    return {_norm(m) for m in NUM.findall(text)}


def _strings(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)
    elif obj is not None:
        yield str(obj)


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.problems, self.text = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED:
            self.problems.append(f"тег <{tag}> Telegram не поддерживает")
        if tag == "a" and not dict(attrs).get("href"):
            self.problems.append("ссылка <a> без href")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"лишний или не по порядку закрытый </{tag}>")
        else:
            self.stack.pop()

    def handle_data(self, data):
        self.text.append(data)


def visible(text: str) -> str:
    p = _Tags()
    p.feed(text)
    return "".join(p.text)


def check(text: str, facts: dict, with_card: bool = True, used_endings: list[str] | None = None) -> list[str]:
    """Список проблем; пустой — текст можно отправлять. used_endings — уже использованные концовки."""
    problems = []
    if not text.strip():
        return ["пустой текст"]
    p = _Tags()
    p.feed(text)
    p.close()
    problems += p.problems
    if p.stack:
        problems.append("не закрыты теги: " + ", ".join(f"<{t}>" for t in p.stack))
    shown = "".join(p.text)
    limit = CAPTION_LIMIT if with_card else TEXT_LIMIT
    if len(shown) > limit:
        problems.append(f"длина {len(shown)} знаков, лимит {limit}")
    known = set()
    for s in _strings(facts):
        known |= numbers(s)
    hrefs = " ".join(re.findall(r'href="([^"]*)"', text))
    for n in sorted(numbers(shown) - known):
        if n.isdigit() and int(n) <= SMALL:
            continue
        if n in numbers(hrefs):
            continue
        problems.append(f"число {n} не из фактов")
    if re.search(r"(^|\s)#\w", shown):
        problems.append("хэштеги не ставим")
    if EMOJI.search(shown):
        problems.append("эмодзи не ставим")
    if used_endings:
        from .endings import clashes, ending
        last = ending(shown)
        for u in clashes(last, used_endings) if last else []:
            problems.append(f"концовка повторяет уже использованную: «{u}»")
    mk = facts.get("facts", {}).get("market") or {}
    if mk.get("market_price_per_m2") and numbers(mk["market_price_per_m2"]) & numbers(shown) \
            and mk.get("market_source", "").lower() not in shown.lower():
        problems.append(f"рыночная цена без источника ({mk.get('market_source')})")
    if mk.get("moscow_price_per_m2") and numbers(mk["moscow_price_per_m2"]) & numbers(shown) \
            and mk.get("moscow_source", "").lower() not in shown.lower():
        problems.append(f"цена по Москве без источника ({mk.get('moscow_source')})")
    for url in re.findall(r'href="([^"]*)"', text):
        if url not in set(_strings(facts)):
            problems.append(f"ссылка не из фактов: {url}")
    return problems


def load_facts(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
