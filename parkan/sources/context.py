"""Контекстные факторы: дорожные события/перекрытия и почасовая погода (CSV/JSON)."""

from __future__ import annotations

from ..db import bulk_insert
from ..timeutil import parse_ts
from .tabular import clean, read_records, to_float

ROAD_COLUMNS = {
    "event_id": "VARCHAR", "event_type": "VARCHAR", "starts_at": "TIMESTAMP", "ends_at": "TIMESTAMP",
    "latitude": "DOUBLE", "longitude": "DOUBLE", "municipality_or_area": "VARCHAR",
    "description": "VARCHAR", "source_name": "VARCHAR",
}
WEATHER_COLUMNS = {
    "hour_start": "TIMESTAMP", "temperature_c": "DOUBLE", "precipitation_mm": "DOUBLE",
    "source_name": "VARCHAR",
}


def import_road_events(con, path, source_name: str = "manual") -> tuple[int, list]:
    rows, rejected = {}, []
    for i, r in enumerate(read_records(path), start=2):
        try:
            eid, starts = clean(r.get("event_id")), parse_ts(r.get("starts_at"))
            if not eid or not starts:
                raise ValueError("нужны event_id и starts_at")
            ends = parse_ts(r.get("ends_at"))
            if ends and ends < starts:
                raise ValueError("ends_at раньше starts_at")
            rows[eid] = (eid, clean(r.get("event_type")), starts, ends, to_float(r.get("latitude")),
                         to_float(r.get("longitude")), clean(r.get("municipality_or_area")),
                         clean(r.get("description")), source_name)
        except ValueError as e:
            rejected.append((i, str(e)))
    return bulk_insert(con, "road_events", ROAD_COLUMNS, rows.values(), replace=True), rejected


def import_weather(con, path, source_name: str = "manual") -> tuple[int, list]:
    rows, rejected = {}, []
    for i, r in enumerate(read_records(path), start=2):
        try:
            hour = parse_ts(r.get("hour_start"))
            if hour is None:
                raise ValueError("нет hour_start")
            hour = hour.replace(minute=0, second=0)
            rows[hour] = (hour, to_float(r.get("temperature_c")), to_float(r.get("precipitation_mm")),
                          source_name)
        except ValueError as e:
            rejected.append((i, str(e)))
    return bulk_insert(con, "weather_hourly", WEATHER_COLUMNS, rows.values(), replace=True), rejected
