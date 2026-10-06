"""Импорт обезличенных событий нарушений из CSV/JSON (выгрузки ЦОДД/АМПП/МАДИ и т. п.).

Ожидаемые поля (см. README): violation_id_or_anonymized_id, source_system,
violation_type, status, occurred_at — обязательные; остальное по возможности.
Файлы с персональными данными (госномер, ФИО, фото и т. п.) не принимаются.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..db import bulk_insert
from ..timeutil import now_msk, parse_ts
from .tabular import clean, read_records, to_float

STATUSES = {
    "сообщение": "сообщение", "обращение": "сообщение", "report": "сообщение", "reported": "сообщение",
    "проверено": "проверено", "подтверждено": "проверено", "verified": "проверено", "confirmed": "проверено",
    "постановление": "постановление", "штраф": "постановление", "ruling": "постановление", "fine": "постановление",
    "отклонено": "отклонено", "rejected": "отклонено", "declined": "отклонено",
}

ALIASES = {
    "violation_id_or_anonymized_id": "violation_id",
    "anonymized_id": "violation_id",
    "id": "violation_id",
}

REQUIRED = ("violation_id", "source_system", "violation_type", "status", "occurred_at")

PII_RE = re.compile(
    r"(госномер|гос_номер|грз|номер_тс|plate|license|licence|\bvin\b|фио|\bfio\b|full_?name|"
    r"owner|владел|photo|фото|passport|паспорт|phone|телефон|e-?mail|snils|снилс|inn\b)",
    re.I,
)

# Грубая рамка Москвы с Новой Москвой — ловит перепутанные широту/долготу.
MOSCOW_BBOX = (55.1, 56.1, 36.7, 38.0)

COLUMNS = {
    "source_system": "VARCHAR", "violation_id": "VARCHAR", "violation_type": "VARCHAR",
    "status": "VARCHAR", "occurred_at": "TIMESTAMP", "recorded_at": "TIMESTAMP",
    "latitude": "DOUBLE", "longitude": "DOUBLE", "street_segment_id": "VARCHAR",
    "parking_zone_id": "VARCHAR", "administrative_district": "VARCHAR",
    "municipality_or_area": "VARCHAR", "vehicle_category": "VARCHAR",
    "resolution_at": "TIMESTAMP", "source_file": "VARCHAR", "loaded_at": "TIMESTAMP",
}


class PiiColumnsError(ValueError):
    def __init__(self, columns):
        super().__init__("файл содержит поля с персональными данными: " + ", ".join(columns)
                         + ". Уберите их из выгрузки или запустите импорт с --drop-pii.")
        self.columns = columns


@dataclass
class ImportResult:
    inserted: int = 0
    rejected: list = field(default_factory=list)  # (номер строки, причина)
    dropped_columns: list = field(default_factory=list)


def find_pii_columns(columns) -> list[str]:
    return [c for c in columns if PII_RE.search(c)]


def normalize_record(rec: dict) -> dict:
    rec = {ALIASES.get(k, k): v for k, v in rec.items()}
    missing = [c for c in REQUIRED if clean(rec.get(c)) is None]
    if missing:
        raise ValueError("нет обязательных полей: " + ", ".join(missing))
    status = STATUSES.get(clean(rec["status"]).lower())
    if status is None:
        raise ValueError(f"неизвестный статус {rec['status']!r}")
    lat, lon = to_float(rec.get("latitude")), to_float(rec.get("longitude"))
    if (lat is None) != (lon is None):
        raise ValueError("координата указана не полностью")
    if lat is not None:
        lat_min, lat_max, lon_min, lon_max = MOSCOW_BBOX
        if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
            raise ValueError(f"координата вне Москвы ({lat}, {lon}); возможно, перепутаны широта и долгота")
    occurred = parse_ts(rec["occurred_at"])
    resolution = parse_ts(rec.get("resolution_at"))
    if resolution and resolution < occurred:
        raise ValueError("resolution_at раньше occurred_at")
    return {
        "source_system": clean(rec["source_system"]),
        "violation_id": clean(rec["violation_id"]),
        "violation_type": clean(rec["violation_type"]),
        "status": status,
        "occurred_at": occurred,
        "recorded_at": parse_ts(rec.get("recorded_at")),
        "latitude": lat,
        "longitude": lon,
        "street_segment_id": clean(rec.get("street_segment_id")),
        "parking_zone_id": clean(rec.get("parking_zone_id")),
        "administrative_district": clean(rec.get("administrative_district")),
        "municipality_or_area": clean(rec.get("municipality_or_area")),
        "vehicle_category": clean(rec.get("vehicle_category")),
        "resolution_at": resolution,
    }


def import_violations(con, path: str | Path, *, drop_pii: bool = False, strict: bool = False) -> ImportResult:
    """Повторный импорт той же записи (source_system + violation_id) обновляет её."""
    records = read_records(path)
    result = ImportResult()
    pii = find_pii_columns(records[0].keys()) if records else []
    if pii:
        if not drop_pii:
            raise PiiColumnsError(pii)
        result.dropped_columns = pii
        records = [{k: v for k, v in r.items() if k not in pii} for r in records]
    loaded_at = now_msk()
    rows = {}
    for line_no, rec in enumerate(records, start=2):  # строка 1 — заголовок
        try:
            n = normalize_record(rec)
        except ValueError as e:
            result.rejected.append((line_no, str(e)))
            continue
        n["source_file"] = Path(path).name
        n["loaded_at"] = loaded_at
        rows[(n["source_system"], n["violation_id"])] = tuple(n[c] for c in COLUMNS)
    if strict and result.rejected:
        raise ValueError(f"{len(result.rejected)} строк отклонено; первая: строка "
                         f"{result.rejected[0][0]}: {result.rejected[0][1]}")
    result.inserted = bulk_insert(con, "violation_events", COLUMNS, rows.values(), replace=True)
    return result
