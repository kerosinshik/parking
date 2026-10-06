"""Синхронизация: полный снимок набора -> сравнение с журналом -> новые события."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from ..db import bulk_insert
from ..timeutil import now_msk
from .store import current_sql, write_events
from .streams import Dataset

SNAP_COLUMNS = {"record_id": "VARCHAR", "hash": "VARCHAR", "payload": "VARCHAR", "geometry": "VARCHAR"}


class IncompleteSnapshot(RuntimeError):
    """Снимок неполный — синхронизация прервана, чтобы не записать ложные удаления."""


@dataclass
class SyncResult:
    dataset_id: int
    expected: int
    fetched: int
    added: int = 0
    changed: int = 0
    removed: int = 0
    file: str | None = None
    changed_fields: dict = field(default_factory=dict)   # поле -> сколько раз менялось


def normalize_feature(feature: dict, ds: Dataset) -> tuple[str, str, str, str | None]:
    attrs = dict((feature.get("properties") or {}).get("attributes") or {})
    for k in ds.drop:
        attrs.pop(k, None)
    rid = attrs.get("global_id") or attrs.get(ds.key)
    if rid in (None, ""):
        raise ValueError(f"у записи нет global_id и {ds.key}")
    payload = json.dumps(attrs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    geom = feature.get("geometry")
    geometry = json.dumps(geom, separators=(",", ":")) if geom else None
    digest = hashlib.sha1((payload + "|" + (geometry or "")).encode()).hexdigest()
    return str(rid), digest, payload, geometry


def fetch_snapshot(con, client, ds: Dataset, *, workers: int = 4, max_pages: int | None = None) -> tuple[int, int]:
    """Загружает все страницы /features во временную таблицу _snap. Возвращает (ожидалось, получено)."""
    expected = client.count(ds.id)
    size = client.page_size
    skips = list(range(0, expected, size))
    if max_pages is not None:
        skips = skips[:max_pages]
    cols = ", ".join(f"{k} {t}" for k, t in SNAP_COLUMNS.items())
    con.execute(f"CREATE OR REPLACE TEMP TABLE _snap ({cols})")
    fetched = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for feats in pool.map(lambda s: client.features_page(ds.id, s), skips):
            rows = [normalize_feature(f, ds) for f in feats]
            bulk_insert(con, "_snap", SNAP_COLUMNS, rows)
            fetched += len(rows)
    # дубликаты идентификатора (бывают при сдвиге страниц во время загрузки) — оставляем один
    con.execute("CREATE OR REPLACE TEMP TABLE _snap AS SELECT * FROM _snap QUALIFY row_number() "
                "OVER (PARTITION BY record_id ORDER BY hash) = 1")
    return expected, fetched


def _changed_fields(old: str, new: str) -> list[str]:
    a, b = json.loads(old), json.loads(new)
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def sync_dataset(con, client, ds: Dataset, state_dir, *, observed_at: datetime | None = None,
                 workers: int = 4, min_ratio: float = 0.98, max_pages: int | None = None) -> SyncResult:
    observed_at = observed_at or now_msk()
    expected, fetched = fetch_snapshot(con, client, ds, workers=workers, max_pages=max_pages)
    unique = con.execute("SELECT count(*) FROM _snap").fetchone()[0]
    res = SyncResult(ds.id, expected, unique)
    if max_pages is None and unique < min_ratio * expected:
        raise IncompleteSnapshot(f"набор {ds.id}: получено {unique} из {expected} записей — "
                                 "изменения не записаны")
    cur = current_sql(state_dir, ds.id)
    if cur is None:
        con.execute("CREATE OR REPLACE TEMP TABLE _cur (record_id VARCHAR, hash VARCHAR, payload VARCHAR)")
    else:
        con.execute(f"CREATE OR REPLACE TEMP TABLE _cur AS SELECT record_id, hash, payload FROM ({cur})")
    events = []
    for rid, h, p, g in con.execute(
            "SELECT s.record_id, s.hash, s.payload, s.geometry FROM _snap s "
            "LEFT JOIN _cur c USING (record_id) WHERE c.record_id IS NULL").fetchall():
        events.append((ds.id, rid, observed_at, "added", h, None, p, g))
    res.added = len(events)
    for rid, h, p, g, old in con.execute(
            "SELECT s.record_id, s.hash, s.payload, s.geometry, c.payload FROM _snap s "
            "JOIN _cur c USING (record_id) WHERE s.hash <> c.hash").fetchall():
        fields = _changed_fields(old, p) if old else []
        for f in fields:
            res.changed_fields[f] = res.changed_fields.get(f, 0) + 1
        events.append((ds.id, rid, observed_at, "changed", h, json.dumps(fields, ensure_ascii=False), p, g))
        res.changed += 1
    if max_pages is None:   # удаления видны только по полному снимку
        for (rid,) in con.execute("SELECT c.record_id FROM _cur c LEFT JOIN _snap s USING (record_id) "
                                  "WHERE s.record_id IS NULL").fetchall():
            events.append((ds.id, rid, observed_at, "removed", None, None, None, None))
            res.removed += 1
    path = write_events(con, state_dir, ds.id, observed_at, events)
    res.file = str(path) if path else None
    con.execute("DROP TABLE IF EXISTS _snap")
    con.execute("DROP TABLE IF EXISTS _cur")
    return res
