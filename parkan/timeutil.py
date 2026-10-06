"""Разбор времени.

Все времена в базе хранятся как наивные TIMESTAMP в московском времени.
Значения со смещением (`Z`, `+03:00`, ...) переводятся в Москву,
значения без смещения считаются московскими.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

_FORMATS = (
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
)


def parse_ts(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        dt = None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            for fmt in _FORMATS:
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            raise ValueError(f"не удалось разобрать время: {text!r}")
    if dt.tzinfo is not None:
        dt = dt.astimezone(MSK).replace(tzinfo=None)
    return dt.replace(microsecond=0)


def now_msk() -> datetime:
    return datetime.now(MSK).replace(tzinfo=None, microsecond=0)
