"""Портал открытых данных Москвы: набор № 623 (парковки) через apidata.mos.ru.

Документация API: https://apidata.mos.ru/Docs
  GET /v1/datasets/{id}/version         — номер версии набора
  GET /v1/datasets/{id}/rows?$top=&$skip= — строки ({global_id, Number, Cells: {...}})

Ключ API берётся из переменной окружения MOS_API_KEY.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse

from ..db import bulk_insert
from ..geo import centroid, edge_length_m
from ..timeutil import now_msk
from .http import get_json

BASE_URL = "https://apidata.mos.ru/v1"
PARKING_DATASET_ID = 623

# Имена полей набора могут отличаться между версиями паспорта набора,
# поэтому для каждого целевого поля перечислены варианты. Первый найденный выигрывает.
FIELD_CANDIDATES = {
    "zone_id": ["ParkingZoneNumber", "ZoneNumber", "ParkingZone", "Zone"],
    "name": ["ParkingName", "Name", "CommonName"],
    "address": ["Address", "Location", "ParkingAddress", "Street"],
    "capacity_total": ["CarCapacity", "Capacity", "ParkingCapacity", "CountSpaces", "SpacesCount"],
    "capacity_disabled": ["CarCapacityDisabled", "CapacityDisabled", "DisabledCapacity",
                          "CountSpacesDisabled"],
    "operating_hours": ["WorkingHours", "OperatingHours", "Schedule"],
    "price": ["Tariffs", "Price", "Tariff", "Cost"],
    "administrative_district": ["AdmArea", "AdmDistrict"],
    "municipality_or_area": ["District", "Area", "Municipality"],
    "latitude": ["Latitude_WGS84", "Latitude", "Lat"],
    "longitude": ["Longitude_WGS84", "Longitude", "Lon"],
    "geometry": ["geoData", "geodata", "Geometry"],
}
_KNOWN = {name for names in FIELD_CANDIDATES.values() for name in names} | {"global_id", "ID", "Number"}

SEGMENT_COLUMNS = {
    "parking_id": "VARCHAR", "zone_id": "VARCHAR", "name": "VARCHAR", "address": "VARCHAR",
    "latitude": "DOUBLE", "longitude": "DOUBLE", "geometry": "VARCHAR", "edge_length_m": "DOUBLE",
    "capacity_total": "INTEGER", "capacity_disabled": "INTEGER", "operating_hours": "VARCHAR",
    "price": "VARCHAR", "administrative_district": "VARCHAR", "municipality_or_area": "VARCHAR",
    "extra": "VARCHAR", "row_hash": "VARCHAR", "source_version": "VARCHAR",
    "valid_from": "TIMESTAMP", "valid_to": "TIMESTAMP",
}


class MosDataClient:
    def __init__(self, api_key: str, *, base_url: str = BASE_URL, page_size: int = 1000,
                 transport=None, sleep=None):
        if not api_key:
            raise ValueError("нужен ключ API data.mos.ru (переменная MOS_API_KEY)")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self._kw = {"transport": transport}
        if sleep is not None:
            self._kw["sleep"] = sleep

    def _url(self, path: str, **params) -> str:
        params["api_key"] = self.api_key
        return f"{self.base_url}{path}?{urllib.parse.urlencode(params, safe='$')}"

    def version(self, dataset_id: int = PARKING_DATASET_ID) -> dict:
        data, _ = get_json(self._url(f"/datasets/{dataset_id}/version"), **self._kw)
        return data

    def _pages(self, path: str, extract):
        skip = 0
        while True:
            page, _ = get_json(self._url(path, **{"$top": self.page_size, "$skip": skip}), **self._kw)
            items = extract(page)
            yield from items
            if len(items) < self.page_size:
                return
            skip += self.page_size

    def iter_rows(self, dataset_id: int = PARKING_DATASET_ID):
        """Строки без геометрии: {global_id, Number, Cells}."""
        def extract(page):
            if not isinstance(page, list):
                raise ValueError(f"ожидался список строк, получено: {type(page).__name__}")
            return page
        return self._pages(f"/datasets/{dataset_id}/rows", extract)

    def iter_features(self, dataset_id: int = PARKING_DATASET_ID):
        """Строки с геометрией (GeoJSON) в формате rows: {global_id, Cells: {..., geoData}}.

        /rows у набора № 623 не отдаёт координаты, поэтому основной способ — /features.
        """
        def extract(page):
            if not isinstance(page, dict) or not isinstance(page.get("features"), list):
                raise ValueError("ожидалась коллекция GeoJSON (features)")
            return [feature_to_row(f) for f in page["features"]]
        return self._pages(f"/datasets/{dataset_id}/features", extract)


def feature_to_row(feature: dict) -> dict:
    attrs = dict((feature.get("properties") or {}).get("attributes") or {})
    if feature.get("geometry"):
        attrs["geoData"] = feature["geometry"]
    return {"global_id": attrs.get("global_id"), "Cells": attrs}


def version_label(v) -> str:
    if isinstance(v, dict):
        parts = [str(v[k]) for k in ("VersionNumber", "ReleaseNumber") if v.get(k) is not None]
        return ".".join(parts) or json.dumps(v, ensure_ascii=False, sort_keys=True)
    return str(v)


def _pick(cells: dict, field: str):
    for name in FIELD_CANDIDATES[field]:
        if name in cells and cells[name] not in (None, ""):
            return cells[name]
    return None


def _text(v):
    if v is None:
        return None
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return str(v).strip() or None


def _int(v):
    if v in (None, ""):
        return None
    try:
        return int(float(str(v).replace(",", ".").replace(" ", "")))
    except ValueError:
        return None


def _float(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def normalize_row(row: dict) -> dict:
    """Строка API ({global_id, Cells}) или плоская строка выгрузки -> нормализованный сегмент."""
    cells = row.get("Cells") if isinstance(row.get("Cells"), dict) else row
    pid = row.get("global_id") or cells.get("global_id") or cells.get("ID")
    if pid in (None, ""):
        raise ValueError("у строки нет global_id")
    geom = _pick(cells, "geometry")
    if isinstance(geom, str):
        try:
            geom = json.loads(geom)
        except ValueError:
            geom = None
    lat, lon = _float(_pick(cells, "latitude")), _float(_pick(cells, "longitude"))
    if (lat is None or lon is None) and isinstance(geom, dict):
        c = centroid(geom)
        if c:
            lat, lon = c
    edge = edge_length_m(geom) if isinstance(geom, dict) else None
    seg = {
        "parking_id": str(pid),
        "zone_id": _text(_pick(cells, "zone_id")),
        "name": _text(_pick(cells, "name")),
        "address": _text(_pick(cells, "address")),
        "latitude": lat,
        "longitude": lon,
        "geometry": json.dumps(geom, ensure_ascii=False) if isinstance(geom, dict) else None,
        "edge_length_m": round(edge, 1) if edge else None,
        "capacity_total": _int(_pick(cells, "capacity_total")),
        "capacity_disabled": _int(_pick(cells, "capacity_disabled")),
        "operating_hours": _text(_pick(cells, "operating_hours")),
        "price": _text(_pick(cells, "price")),
        "administrative_district": _text(_pick(cells, "administrative_district")),
        "municipality_or_area": _text(_pick(cells, "municipality_or_area")),
        "extra": _text({k: v for k, v in cells.items() if k not in _KNOWN}) if cells else None,
    }
    seg["row_hash"] = hashlib.sha256(
        json.dumps(seg, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return seg


def load_segments(con, rows, source_version: str, loaded_at=None) -> dict:
    """Загружает полный снимок набора, ведя историю версий (SCD2) и журнал изменений.

    Ожидается ПОЛНЫЙ снимок: сегменты, которых в нём нет, считаются удалёнными.
    """
    loaded_at = loaded_at or now_msk()
    new: dict[str, dict] = {}
    errors = []
    for i, row in enumerate(rows):
        try:
            seg = normalize_row(row)
        except ValueError as e:
            errors.append((i, str(e)))
            continue
        new[seg["parking_id"]] = seg
    current = dict(con.execute("SELECT parking_id, row_hash FROM current_segments").fetchall())

    added = [p for p in new if p not in current]
    modified = [p for p in new if p in current and current[p] != new[p]["row_hash"]]
    removed = [p for p in current if p not in new]

    con.execute("BEGIN")
    try:
        to_close = modified + removed
        if to_close:
            con.execute(
                "UPDATE parking_segments SET valid_to = ? WHERE valid_to IS NULL AND parking_id IN "
                "(SELECT unnest(?::VARCHAR[]))", [loaded_at, to_close])
        bulk_insert(con, "parking_segments", SEGMENT_COLUMNS, [
            tuple({**new[p], "source_version": source_version, "valid_from": loaded_at,
                   "valid_to": None}[c] for c in SEGMENT_COLUMNS)
            for p in added + modified
        ])
        changes = ([(p, "added") for p in added] + [(p, "modified") for p in modified]
                   + [(p, "removed") for p in removed])
        bulk_insert(con, "parking_changes",
                    {"parking_id": "VARCHAR", "change_type": "VARCHAR",
                     "source_version": "VARCHAR", "changed_at": "TIMESTAMP"},
                    [(p, t, source_version, loaded_at) for p, t in changes])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return {"rows": len(new), "added": len(added), "modified": len(modified),
            "removed": len(removed), "unchanged": len(new) - len(added) - len(modified),
            "errors": errors}


def rows_from_file_payload(payload) -> tuple[list, str | None]:
    """Файл может быть списком строк или снимком {"version": ..., "rows": [...]}."""
    if isinstance(payload, dict) and "rows" in payload:
        return payload["rows"], (version_label(payload["version"]) if payload.get("version") else None)
    if isinstance(payload, list):
        return payload, None
    raise ValueError("ожидался список строк или объект {version, rows}")
