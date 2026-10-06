import json

import pytest

from parkan import db


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    yield c
    c.close()


def segment_row(gid, lat, lon, cap=10, area="район Тест", length=0.001, **cells):
    return {"global_id": gid, "Number": 1, "Cells": {
        "global_id": gid, "ParkingName": f"P{gid}", "ParkingZoneNumber": "4001",
        "AdmArea": "ЦАО", "District": area, "Address": f"ул. Тестовая, {gid}",
        "CarCapacity": cap, "CarCapacityDisabled": 1,
        "geoData": {"type": "LineString", "coordinates": [[lon - length / 2, lat], [lon + length / 2, lat]]},
        **cells}}


def write_csv(path, header, rows, sep=",", encoding="utf-8"):
    lines = [sep.join(header)] + [sep.join("" if v is None else str(v) for v in r) for r in rows]
    path.write_bytes(("\n".join(lines) + "\n").encode(encoding))
    return path


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path
