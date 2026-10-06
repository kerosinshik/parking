"""Чтение табличных файлов (CSV с любым разделителем, UTF-8/CP1251, JSON)."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("не удалось определить кодировку (ожидается UTF-8 или CP1251)")


def read_records(path: str | Path) -> list[dict]:
    """Список словарей; ключи приведены к нижнему регистру без пробелов по краям."""
    path = Path(path)
    text = decode(path.read_bytes())
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), None)
        if not isinstance(data, list):
            raise ValueError("JSON должен содержать список записей")
        records = data
    else:
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        records = list(csv.DictReader(io.StringIO(text), dialect=dialect))
    return [{str(k).strip().lower(): v for k, v in r.items() if k is not None} for r in records]


def clean(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def to_float(v):
    v = clean(v)
    if v is None:
        return None
    return float(str(v).replace(",", "."))


def to_int(v):
    v = clean(v)
    if v is None:
        return None
    f = float(str(v).replace(",", ".").replace(" ", ""))
    if f != int(f):
        raise ValueError(f"ожидалось целое число: {v!r}")
    return int(f)
