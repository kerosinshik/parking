"""Финальные ироничные фразы постов: журнал отправленных и проверка на повтор.

Шутка, которая повторилась, хуже, чем никакой. Поэтому каждая отправленная концовка записывается
в channel_state/endings.json, перед написанием текстов журнал кладётся в папку дня (endings.txt),
а channel-lint не пропускает концовку, похожую на уже использованную или на пример из навыка.
Похожая — почти та же фраза (сходство строк от 0,7) или общий кусок из 4 слов подряд
(«…ещё никогда не было так просто»).
"""

from __future__ import annotations

import json
import re
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

SIMILAR = 0.7
SHINGLE = 4
SKILL = Path(__file__).resolve().parents[2] / ".claude" / "skills" / "channel-copy" / "SKILL.md"


def ending(visible_text: str) -> str | None:
    """Последний абзац поста — финальная фраза (если это не абзац со ссылкой на лот)."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", visible_text.strip()) if p.strip()]
    if len(paras) < 2:
        return None
    return paras[-1]


def _words(s: str) -> list[str]:
    return re.findall(r"[a-zа-яё0-9]+", s.lower().replace("ё", "е"))


def _shingles(s: str) -> set[tuple]:
    w = _words(s)
    return {tuple(w[i:i + SHINGLE]) for i in range(len(w) - SHINGLE + 1)}


def similar(a: str, b: str) -> bool:
    na, nb = " ".join(_words(a)), " ".join(_words(b))
    if not na or not nb:
        return False
    return SequenceMatcher(None, na, nb).ratio() >= SIMILAR or bool(_shingles(a) & _shingles(b))


def skill_examples() -> list[str]:
    """Фразы-примеры из навыка: это образцы интонации, а не готовые концовки."""
    if not SKILL.exists():
        return []
    text = SKILL.read_text(encoding="utf-8")
    start = text.find("## Финальная фраза")
    end = text.find("\n## ", start + 1)
    return re.findall(r"«([^»]{15,})»", text[start:end] if start != -1 else "")


def load(path) -> list[dict]:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def record(path, phrases: list[str], day: date) -> None:
    if not phrases:
        return
    items = load(path) + [{"date": day.isoformat(), "text": t} for t in phrases]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def clashes(phrase: str, used: list[str]) -> list[str]:
    return [u for u in used if similar(phrase, u)]


def write_list(path, used: list[dict]) -> None:
    """endings.txt в папке дня: что уже было — чтобы автор текста не повторялся."""
    lines = ["Уже использованные финальные фразы — не повторять ни дословно, ни по смыслу и приёму.", ""]
    lines += [f"{u['date']}  {u['text']}" for u in used]
    lines += ["", "Примеры из навыка — тоже не использовать дословно:"] + [f"- {e}" for e in skill_examples()]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
