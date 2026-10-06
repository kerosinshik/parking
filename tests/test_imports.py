import json
from datetime import datetime

import pytest

from parkan.sources import context, mosdata, occupancy, violations

from .conftest import segment_row, write_csv, write_json

VH = ["violation_id_or_anonymized_id", "source_system", "violation_type", "status", "occurred_at",
      "latitude", "longitude"]


def test_violations_import_normalizes_and_upserts(con, tmp_path):
    f = write_csv(tmp_path / "v.csv", VH, [
        ["a1", "МАДИ", "стоянка на тротуаре", "Постановление", "2026-09-07 12:05:00", "55.75", "37.6"],
        ["a2", "АМПП", "неоплата", "verified", "2026-09-07T09:05:00Z", "", ""],   # UTC -> МСК
        ["a3", "АМПП", "неоплата", "непонятно", "2026-09-07 12:00", "", ""],
        ["a4", "АМПП", "неоплата", "сообщение", "2026-09-07 12:00", "37.6", "55.75"],  # перепутаны
        ["", "АМПП", "неоплата", "сообщение", "2026-09-07 12:00", "", ""],
    ])
    r = violations.import_violations(con, f)
    assert r.inserted == 2
    assert [line for line, _ in r.rejected] == [4, 5, 6]
    assert "статус" in r.rejected[0][1] and "вне Москвы" in r.rejected[1][1] and "обязательных" in r.rejected[2][1]
    rows = dict(con.execute("SELECT violation_id, occurred_at FROM violation_events").fetchall())
    assert rows["a2"] == datetime(2026, 9, 7, 12, 5)
    assert con.execute("SELECT status FROM violation_events WHERE violation_id='a1'").fetchone()[0] == "постановление"

    violations.import_violations(con, f)  # повторный импорт не дублирует
    assert con.execute("SELECT count(*) FROM violation_events").fetchone()[0] == 2

    with pytest.raises(ValueError):
        violations.import_violations(con, f, strict=True)


def test_violations_refuse_pii_unless_dropped(con, tmp_path):
    f = write_csv(tmp_path / "v.csv", VH + ["Госномер", "photo_url"], [
        ["a1", "МАДИ", "тротуар", "проверено", "07.09.2026 12:05", "55.75", "37.6", "А123ВС77", "http://x"],
    ], sep=";", encoding="cp1251")
    with pytest.raises(violations.PiiColumnsError) as e:
        violations.import_violations(con, f)
    assert set(e.value.columns) == {"госномер", "photo_url"}
    assert con.execute("SELECT count(*) FROM violation_events").fetchone()[0] == 0
    r = violations.import_violations(con, f, drop_pii=True)
    assert r.inserted == 1 and set(r.dropped_columns) == {"госномер", "photo_url"}


def test_occupancy_import_derives_and_validates(con, tmp_path):
    mosdata.load_segments(con, [segment_row(1, 55.75, 37.6, cap=10)], "1")
    f = write_csv(tmp_path / "o.csv", ["parking_id", "measured_at", "total_spaces", "occupied_spaces", "free_spaces"], [
        [1, "2026-09-07 12:00", "", 7, ""],     # total из справочника
        [1, "2026-09-07 12:15", "", "", 1],     # occupied из free
        [1, "2026-09-07 12:30", 10, 12, ""],    # больше вместимости
        [2, "2026-09-07 12:00", "", 3, ""],     # неизвестная парковка без total
        [2, "2026-09-07 12:15", "", 3, 2],      # total = occupied + free
    ])
    r = occupancy.import_occupancy_file(con, f)
    assert r.inserted == 3 and [x[0] for x in r.rejected] == [3, 4]
    got = con.execute("SELECT parking_id, total_spaces, occupied_spaces, free_spaces FROM occupancy_snapshots "
                      "ORDER BY parking_id, measured_at").fetchall()
    assert got == [("1", 10, 7, 3), ("1", 10, 9, 1), ("2", 5, 3, 2)]
    with pytest.raises(ValueError):
        occupancy.import_occupancy_file(con, f, source_type="scraped")


def test_http_occupancy_source_with_mapping(con, tmp_path):
    mosdata.load_segments(con, [segment_row(1, 55.75, 37.6, cap=10)], "1")
    body = json.dumps({"items": [{"id": 1, "ts": "2026-09-07T12:00:00+03:00", "data": {"free": 4}}]}).encode()
    seen = {}

    def transport(url, headers=None):
        seen["url"], seen["headers"] = url, headers
        return 200, body

    src = occupancy.HttpJsonOccupancySource(
        "https://example.test/occ", source_name="ampp", records_key="items", transport=transport,
        headers={"Authorization": "Bearer T"},
        mapping=occupancy.parse_mapping("parking_id=id,measured_at=ts,free_spaces=data.free"))
    r = src.collect(con, raw_dir=tmp_path)
    assert r.inserted == 1 and seen["headers"] == {"Authorization": "Bearer T"}
    assert con.execute("SELECT occupied_spaces, source_type FROM occupancy_snapshots").fetchone() == (6, "official_api")
    assert con.execute("SELECT count(*) FROM raw_files").fetchone()[0] == 1
    with pytest.raises(ValueError):
        occupancy.parse_mapping("bogus=x")


def test_context_imports(con, tmp_path):
    w = write_csv(tmp_path / "w.csv", ["hour_start", "temperature_c", "precipitation_mm"],
                  [["2026-09-07 12:30", "12,5", "0.4"], ["", "1", "1"]], sep=";")
    assert context.import_weather(con, w)[0] == 1
    assert con.execute("SELECT hour_start, temperature_c FROM weather_hourly").fetchone() == (datetime(2026, 9, 7, 12), 12.5)
    r = write_json(tmp_path / "r.json", [{"event_id": "e1", "starts_at": "2026-09-07 10:00", "ends_at": "2026-09-07 09:00"},
                                         {"event_id": "e2", "starts_at": "2026-09-07 10:00"}])
    n, rejected = context.import_road_events(con, r)
    assert n == 1 and len(rejected) == 1
