"""Схема хранилища DuckDB и вспомогательные функции загрузки."""

from __future__ import annotations

import csv
import os
import tempfile
from datetime import datetime
from pathlib import Path

import duckdb

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_files (
    source      VARCHAR NOT NULL,
    sha256      VARCHAR NOT NULL,
    path        VARCHAR NOT NULL,
    fetched_at  TIMESTAMP NOT NULL,
    meta        VARCHAR,
    PRIMARY KEY (source, sha256)
);

-- Справочник парковочных сегментов с историей версий (SCD2):
-- действующая версия имеет valid_to IS NULL.
CREATE TABLE IF NOT EXISTS parking_segments (
    parking_id               VARCHAR NOT NULL,
    zone_id                  VARCHAR,
    name                     VARCHAR,
    address                  VARCHAR,
    latitude                 DOUBLE,
    longitude                DOUBLE,
    geometry                 VARCHAR,
    edge_length_m            DOUBLE,
    capacity_total           INTEGER,
    capacity_disabled        INTEGER,
    operating_hours          VARCHAR,
    price                    VARCHAR,
    administrative_district  VARCHAR,
    municipality_or_area     VARCHAR,
    extra                    VARCHAR,
    row_hash                 VARCHAR NOT NULL,
    source_version           VARCHAR,
    valid_from               TIMESTAMP NOT NULL,
    valid_to                 TIMESTAMP
);

CREATE TABLE IF NOT EXISTS parking_changes (
    parking_id      VARCHAR NOT NULL,
    change_type     VARCHAR NOT NULL,   -- added | modified | removed
    source_version  VARCHAR,
    changed_at      TIMESTAMP NOT NULL
);

CREATE OR REPLACE VIEW current_segments AS
    SELECT * FROM parking_segments WHERE valid_to IS NULL;

CREATE TABLE IF NOT EXISTS occupancy_snapshots (
    parking_id       VARCHAR NOT NULL,
    measured_at      TIMESTAMP NOT NULL,
    total_spaces     INTEGER NOT NULL,
    occupied_spaces  INTEGER NOT NULL,
    free_spaces      INTEGER NOT NULL,
    source_type      VARCHAR NOT NULL,  -- official_api | official_file | manual_import
    source_name      VARCHAR NOT NULL,
    loaded_at        TIMESTAMP NOT NULL,
    PRIMARY KEY (parking_id, measured_at, source_name)
);

CREATE TABLE IF NOT EXISTS violation_events (
    source_system            VARCHAR NOT NULL,
    violation_id             VARCHAR NOT NULL,
    violation_type           VARCHAR NOT NULL,
    status                   VARCHAR NOT NULL,  -- сообщение | проверено | постановление | отклонено
    occurred_at              TIMESTAMP NOT NULL,
    recorded_at              TIMESTAMP,
    latitude                 DOUBLE,
    longitude                DOUBLE,
    street_segment_id        VARCHAR,
    parking_zone_id          VARCHAR,
    administrative_district  VARCHAR,
    municipality_or_area     VARCHAR,
    vehicle_category         VARCHAR,
    resolution_at            TIMESTAMP,
    source_file              VARCHAR,
    loaded_at                TIMESTAMP NOT NULL,
    PRIMARY KEY (source_system, violation_id)
);

CREATE TABLE IF NOT EXISTS road_events (
    event_id                 VARCHAR PRIMARY KEY,
    event_type               VARCHAR,
    starts_at                TIMESTAMP NOT NULL,
    ends_at                  TIMESTAMP,
    latitude                 DOUBLE,
    longitude                DOUBLE,
    municipality_or_area     VARCHAR,   -- NULL = событие на весь город
    description              VARCHAR,
    source_name              VARCHAR
);

CREATE TABLE IF NOT EXISTS weather_hourly (
    hour_start        TIMESTAMP PRIMARY KEY,
    temperature_c     DOUBLE,
    precipitation_mm  DOUBLE,
    source_name       VARCHAR
);
"""


def default_db_path() -> Path:
    return Path(os.environ.get("PARKAN_DB", "data/parkan.duckdb"))


def connect(path: str | Path | None = None) -> duckdb.DuckDBPyConnection:
    path = Path(path) if path is not None else default_db_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(SCHEMA)
    return con


def _csv_value(v):
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return v


def bulk_insert(con, table: str, columns: dict[str, str], rows, *, replace: bool = False) -> int:
    """Быстрая вставка большого числа строк через временный CSV.

    columns — упорядоченный словарь «имя -> тип DuckDB»; rows — кортежи в том же порядке.
    Пустая строка и None сохраняются как NULL.
    """
    rows = list(rows)
    if not rows:
        return 0
    fd, tmp = tempfile.mkstemp(suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(columns)
            for r in rows:
                w.writerow([_csv_value(v) for v in r])
        cols = ", ".join(columns)
        spec = "{" + ", ".join(f"'{k}': '{t}'" for k, t in columns.items()) + "}"
        verb = "INSERT OR REPLACE INTO" if replace else "INSERT INTO"
        con.execute(
            f"{verb} {table} ({cols}) SELECT {cols} FROM read_csv(?, header=true, "
            f"columns={spec}, nullstr='', quote='\"', escape='\"', timestampformat='%Y-%m-%d %H:%M:%S')",
            [tmp],
        )
    finally:
        os.unlink(tmp)
    return len(rows)
