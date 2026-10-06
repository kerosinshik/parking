"""Снимки занятости парковок (occupancy).

Единый интерфейс для трёх типов источника:
  official_api   — опрос HTTP JSON-endpoint, выданного АМПП/ЦОДД (HttpJsonOccupancySource);
  official_file  — регулярная выгрузка CSV/JSON от владельца системы;
  manual_import  — ручная загрузка файла.
Все пути сводятся к normalize_records() -> occupancy_snapshots, поэтому при появлении
официального API архитектуру менять не нужно.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..db import bulk_insert
from ..raw import store_bytes
from ..timeutil import now_msk, parse_ts
from . import SOURCE_TYPES
from .http import get_json
from .tabular import clean, read_records, to_int

COLUMNS = {
    "parking_id": "VARCHAR", "measured_at": "TIMESTAMP", "total_spaces": "INTEGER",
    "occupied_spaces": "INTEGER", "free_spaces": "INTEGER", "source_type": "VARCHAR",
    "source_name": "VARCHAR", "loaded_at": "TIMESTAMP",
}

DEFAULT_MAPPING = {
    "parking_id": "parking_id", "measured_at": "measured_at", "total_spaces": "total_spaces",
    "occupied_spaces": "occupied_spaces", "free_spaces": "free_spaces",
}


@dataclass
class OccupancyResult:
    inserted: int = 0
    rejected: list = field(default_factory=list)


def _get_path(rec: dict, path: str):
    """Достаёт значение по пути вида "a.b.c" (для вложенного JSON)."""
    cur = rec
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part, cur.get(part.lower()))
    return cur


def normalize_records(con, records, *, source_type: str, source_name: str,
                      mapping: dict | None = None) -> OccupancyResult:
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"source_type должен быть одним из {SOURCE_TYPES}")
    mapping = {**DEFAULT_MAPPING, **(mapping or {})}
    capacity = dict(con.execute(
        "SELECT parking_id, capacity_total FROM current_segments WHERE capacity_total IS NOT NULL"
    ).fetchall())
    loaded_at = now_msk()
    result = OccupancyResult()
    rows = {}
    for i, rec in enumerate(records, start=1):
        try:
            pid = clean(_get_path(rec, mapping["parking_id"]))
            if pid is None:
                raise ValueError("нет parking_id")
            pid = str(pid)
            measured = parse_ts(_get_path(rec, mapping["measured_at"]))
            if measured is None:
                raise ValueError("нет measured_at")
            total = to_int(_get_path(rec, mapping["total_spaces"]))
            occupied = to_int(_get_path(rec, mapping["occupied_spaces"]))
            free = to_int(_get_path(rec, mapping["free_spaces"]))
            if total is None:
                total = capacity.get(pid)
                if total is None and occupied is not None and free is not None:
                    total = occupied + free
            if total is None:
                raise ValueError(f"неизвестна вместимость парковки {pid}")
            if total <= 0:
                raise ValueError("total_spaces должно быть > 0")
            if occupied is None and free is None:
                raise ValueError("нужно occupied_spaces или free_spaces")
            if occupied is None:
                occupied = total - free
            if free is None:
                free = total - occupied
            if not (0 <= occupied <= total) or occupied + free != total:
                raise ValueError(f"несогласованные значения: total={total}, occupied={occupied}, free={free}")
        except ValueError as e:
            result.rejected.append((i, str(e)))
            continue
        rows[(pid, measured)] = (pid, measured, total, occupied, free, source_type, source_name, loaded_at)
    result.inserted = bulk_insert(con, "occupancy_snapshots", COLUMNS, rows.values(), replace=True)
    return result


def import_occupancy_file(con, path, *, source_type: str = "manual_import", source_name: str | None = None,
                          mapping: dict | None = None) -> OccupancyResult:
    return normalize_records(con, read_records(path), source_type=source_type,
                             source_name=source_name or str(path).rsplit("/", 1)[-1], mapping=mapping)


class HttpJsonOccupancySource:
    """Опрос официального JSON-endpoint занятости (тип official_api).

    url, заголовки (например, токен) и соответствие полей задаются конфигурацией —
    формат будет известен только после получения доступа от АМПП/ЦОДД.
    records_key — ключ списка в ответе, если ответ не является списком.
    """

    def __init__(self, url: str, *, source_name: str, mapping: dict | None = None,
                 headers: dict | None = None, records_key: str | None = None, transport=None):
        self.url, self.source_name = url, source_name
        self.mapping, self.headers = mapping, headers
        self.records_key, self.transport = records_key, transport

    def collect(self, con, raw_dir=None) -> OccupancyResult:
        data, body = get_json(self.url, headers=self.headers, transport=self.transport)
        store_bytes(con, f"occupancy_{self.source_name}", body, suffix=".json", raw_dir=raw_dir,
                    meta={"url": self.url.split("?")[0]})
        records = data.get(self.records_key) if self.records_key else data
        if not isinstance(records, list):
            raise ValueError("ответ не содержит списка записей; укажите records_key")
        return normalize_records(con, records, source_type="official_api",
                                 source_name=self.source_name, mapping=self.mapping)


def parse_mapping(text: str | None) -> dict:
    """"parking_id=id,free_spaces=data.free" -> dict."""
    if not text:
        return {}
    out = {}
    for part in text.split(","):
        k, _, v = part.partition("=")
        if not v:
            raise ValueError(f"неверный элемент соответствия полей: {part!r}")
        if k.strip() not in DEFAULT_MAPPING:
            raise ValueError(f"неизвестное поле {k!r}; допустимы: {', '.join(DEFAULT_MAPPING)}")
        out[k.strip()] = v.strip()
    return out


def parse_headers(text: str | None) -> dict:
    return json.loads(text) if text else {}
