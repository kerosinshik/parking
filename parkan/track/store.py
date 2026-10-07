"""Журнал изменений в Parquet — единственный источник истины.

state/ds<ID>/events-<время>.parquet — события одного запуска синхронизации:
  added    — запись появилась (payload — её содержимое);
  changed  — содержимое изменилось (payload — новое, changed_fields — какие поля);
  removed  — запись исчезла из набора.
Состояние на любую дату = последнее событие по каждой записи не позже этой даты.
Файлы только добавляются, поэтому журнал удобно хранить в git.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path

from ..db import bulk_insert

EVENT_COLUMNS = {
    "dataset_id": "INTEGER", "record_id": "VARCHAR", "observed_at": "TIMESTAMP", "event": "VARCHAR",
    "hash": "VARCHAR", "changed_fields": "VARCHAR", "payload": "VARCHAR", "geometry": "VARCHAR",
}


def ds_dir(state_dir, dataset_id: int) -> Path:
    return Path(state_dir) / f"ds{dataset_id}"


def event_files(state_dir, dataset_id: int) -> list[Path]:
    d = ds_dir(state_dir, dataset_id)
    return sorted(d.glob("events-*.parquet")) if d.exists() else []


def events_source(state_dir, dataset_id: int) -> str | None:
    """Выражение FROM для всех событий набора или None, если журнала ещё нет."""
    files = event_files(state_dir, dataset_id)
    if not files:
        return None
    lst = ", ".join("'" + f.as_posix().replace("'", "''") + "'" for f in files)
    return f"read_parquet([{lst}])"


def current_sql(state_dir, dataset_id: int, as_of: datetime | None = None) -> str | None:
    """SELECT текущего (или на дату) состояния набора: record_id, hash, payload, geometry, first_seen, last_change."""
    src = events_source(state_dir, dataset_id)
    if src is None:
        return None
    cond = f"WHERE observed_at <= TIMESTAMP '{as_of:%Y-%m-%d %H:%M:%S}'" if as_of else ""
    return f"""
        SELECT record_id, hash, payload, geometry, first_seen, observed_at AS last_change
        FROM (
            SELECT *, min(observed_at) OVER (PARTITION BY record_id) AS first_seen,
                   row_number() OVER (PARTITION BY record_id ORDER BY observed_at DESC) AS rn
            FROM {src} {cond}
        ) WHERE rn = 1 AND event <> 'removed'
    """


def ever_sql(state_dir, dataset_id: int) -> str | None:
    """Все записи, когда-либо виденные в наборе: последнее содержимое и признак gone (запись исчезла).

    Нужен для наборов, которые хранят только открытые записи (аварийные вызовы № 62461):
    по текущему состоянию закрытые вызовы пропадают, и прошлые дни выглядят пустыми.
    """
    src = events_source(state_dir, dataset_id)
    if src is None:
        return None
    return f"""
        SELECT record_id, hash, payload, geometry, first_seen, last_change, gone
        FROM (
            SELECT *, min(observed_at) OVER w AS first_seen, max(observed_at) OVER w AS last_change,
                   arg_max(event, observed_at) OVER w = 'removed' AS gone,
                   row_number() OVER (PARTITION BY record_id
                                      ORDER BY (event = 'removed')::INT, observed_at DESC) AS rn
            FROM {src} WINDOW w AS (PARTITION BY record_id)
        ) WHERE rn = 1
    """


def tracking_start(state_dir, dataset_id: int) -> datetime | None:
    """Время первой синхронизации набора (по имени первого файла журнала)."""
    files = event_files(state_dir, dataset_id)
    return datetime.strptime(files[0].stem.removeprefix("events-"), "%Y%m%dT%H%M%S") if files else None


def write_events(con, state_dir, dataset_id: int, observed_at: datetime, rows) -> Path | None:
    """Пишет события запуска в новый Parquet-файл. Существующие файлы не трогает."""
    rows = list(rows)
    if not rows:
        return None
    d = ds_dir(state_dir, dataset_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"events-{observed_at:%Y%m%dT%H%M%S}.parquet"
    if path.exists():
        raise FileExistsError(f"файл журнала уже существует: {path}")
    cols = ", ".join(f"{k} {t}" for k, t in EVENT_COLUMNS.items())
    con.execute(f"CREATE OR REPLACE TEMP TABLE _ev ({cols})")
    bulk_insert(con, "_ev", EVENT_COLUMNS, rows)
    fd, tmp = tempfile.mkstemp(suffix=".parquet", dir=d)
    os.close(fd)
    os.unlink(tmp)
    con.execute(f"COPY (SELECT * FROM _ev ORDER BY record_id) TO '{Path(tmp).as_posix()}' "
                f"(FORMAT parquet, COMPRESSION zstd)")
    os.replace(tmp, path)   # атомарно: незавершённый файл в журнал не попадает
    con.execute("DROP TABLE _ev")
    return path
