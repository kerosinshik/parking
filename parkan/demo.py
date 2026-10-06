"""Синтетические данные для проверки конвейера целиком.

Генерирует файлы в тех же форматах, что ожидаются от реальных источников:
  mos_623_snapshot.json — снимок набора № 623 в формате apidata.mos.ru (Cells);
  occupancy.csv, violations.csv, weather.csv, road_events.csv.
В данные заложена известная зависимость: нарушений больше, когда загрузка выше 75 %.
Это позволяет проверить, что конвейер действительно её находит.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

AREAS = [
    # район, округ, центр (lat, lon), пиковая загрузка в будни
    ("Арбат", "Центральный административный округ", (55.7505, 37.5925), 0.97),
    ("Хамовники", "Центральный административный округ", (55.7330, 37.5800), 0.82),
]
VIOLATION_TYPES = [
    ("стоянка на тротуаре", 0.35),
    ("неоплата парковки", 0.30),
    ("стоянка в зоне действия знака 3.27", 0.20),
    ("стоянка на месте для инвалидов", 0.10),
    ("стоянка на пешеходном переходе", 0.05),
]
STATUSES = [("постановление", 0.5), ("проверено", 0.25), ("сообщение", 0.15), ("отклонено", 0.10)]
SOURCES = ["МАДИ", "АМПП", "Помощник Москвы"]


def _weighted(rng, items):
    r, acc = rng.random(), 0.0
    for value, w in items:
        acc += w
        if r <= acc:
            return value
    return items[-1][0]


def _poisson(rng, lam):
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= l:
            return k
        k += 1


def daily_rate(ts: datetime, peak: float) -> float:
    """Типичный профиль дня: ночью ~35 %, днём — пик района, в выходные ниже."""
    h = ts.hour + ts.minute / 60
    day = math.exp(-((h - 14.5) / 4.2) ** 2)          # дневной горб с максимумом в 14:30
    rate = 0.35 + (peak - 0.35) * day
    if ts.weekday() >= 5:
        rate -= 0.15 * day
    return rate


def generate(out_dir: str | Path, *, days: int = 14, start: datetime = datetime(2026, 9, 7),
             segments_per_area: int = 20, step_min: int = 15, seed: int = 42) -> dict:
    rng = random.Random(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    segments, rows = [], []
    for a_idx, (area, adm, (clat, clon), peak) in enumerate(AREAS):
        for i in range(segments_per_area):
            gid = 1_000_000 + a_idx * 1000 + i
            # сегменты раскладываются сеткой 5×4 с шагом ~150 м; каждый — отрезок ~60–120 м вдоль улицы
            lat = clat + (i // 5 - 1.5) * 0.00135
            lon = clon + (i % 5 - 2) * 0.0024
            length_deg = rng.uniform(0.0010, 0.0020)
            geom = {"type": "LineString", "coordinates": [[lon, lat], [lon + length_deg, lat + 0.00005]]}
            cap = rng.randint(8, 30)
            segments.append({"id": str(gid), "area": area, "lat": lat, "lon": lon + length_deg / 2,
                             "cap": cap, "peak": peak, "offset": rng.uniform(-0.04, 0.04)})
            rows.append({"global_id": gid, "Number": len(rows) + 1, "Cells": {
                "global_id": gid,
                "ParkingName": f"Парковка {area} №{i + 1}",
                "ParkingZoneNumber": f"{4000 + a_idx * 10 + i // 5}",
                "AdmArea": adm,
                "District": f"район {area}",
                "Address": f"{area}, улица {i // 5 + 1}, участок {i % 5 + 1}",
                "CarCapacity": cap,
                "CarCapacityDisabled": 1 if cap > 15 else 0,
                "Tariffs": [{"TimeRange": "08:00-21:00", "HourPrice": 380 if area == "Арбат" else 250}],
                "WorkingHours": [{"DayWeek": "ежедневно", "Hours": "круглосуточно"}],
                "geoData": geom,
            }})
    (out / "mos_623_snapshot.json").write_text(json.dumps(
        {"version": {"VersionNumber": 3, "ReleaseNumber": 1042}, "rows": rows},
        ensure_ascii=False, indent=1), encoding="utf-8")

    n_occ = n_viol = 0
    vid = 0
    with open(out / "occupancy.csv", "w", newline="", encoding="utf-8") as fo, \
         open(out / "violations.csv", "w", newline="", encoding="utf-8") as fv:
        wo, wv = csv.writer(fo), csv.writer(fv)
        wo.writerow(["parking_id", "measured_at", "total_spaces", "occupied_spaces", "free_spaces"])
        wv.writerow(["violation_id_or_anonymized_id", "source_system", "violation_type", "status",
                     "occurred_at", "recorded_at", "latitude", "longitude", "street_segment_id",
                     "parking_zone_id", "administrative_district", "municipality_or_area",
                     "vehicle_category", "resolution_at"])
        steps = days * 24 * 60 // step_min
        for s in segments:
            for k in range(steps):
                ts = start + timedelta(minutes=k * step_min)
                rate = min(1.0, max(0.0, daily_rate(ts, s["peak"]) + s["offset"] + rng.gauss(0, 0.03)))
                occ = round(rate * s["cap"])
                wo.writerow([s["id"], ts.isoformat(sep=" "), s["cap"], occ, s["cap"] - occ])
                n_occ += 1
                real_rate = occ / s["cap"]
                lam = 0.005 + 0.30 * max(0.0, (real_rate - 0.75) / 0.25) ** 2
                for _ in range(_poisson(rng, lam)):
                    vid += 1
                    at = ts + timedelta(seconds=rng.randint(0, step_min * 60 - 1))
                    status = _weighted(rng, STATUSES)
                    mode = rng.random()
                    lat = lon = seg_id = None
                    if mode < 0.10:               # только идентификатор сегмента
                        seg_id = s["id"]
                    elif mode < 0.13:             # далеко от парковок — не сопоставится
                        lat, lon = s["lat"] + 0.02, s["lon"] + 0.03
                    else:                          # координата рядом с сегментом (±~20 м)
                        lat = s["lat"] + rng.gauss(0, 0.00012)
                        lon = s["lon"] + rng.gauss(0, 0.0002)
                    resolution = (at + timedelta(days=rng.randint(1, 5))) if status == "постановление" else None
                    wv.writerow([
                        hashlib.sha1(f"v{vid}".encode()).hexdigest()[:16], rng.choice(SOURCES),
                        _weighted(rng, VIOLATION_TYPES), status,
                        at.isoformat(sep=" "), (at + timedelta(minutes=rng.randint(1, 30))).isoformat(sep=" "),
                        f"{lat:.6f}" if lat else "", f"{lon:.6f}" if lon else "", seg_id or "", "", "", "",
                        "легковой", resolution.isoformat(sep=" ") if resolution else "",
                    ])
                    n_viol += 1

    with open(out / "weather.csv", "w", newline="", encoding="utf-8") as fw:
        w = csv.writer(fw)
        w.writerow(["hour_start", "temperature_c", "precipitation_mm"])
        for h in range(days * 24):
            ts = start + timedelta(hours=h)
            temp = 12 + 5 * math.sin((ts.hour - 9) / 24 * 2 * math.pi) + rng.gauss(0, 1)
            rain = round(rng.expovariate(1.5), 1) if rng.random() < 0.12 else 0.0
            w.writerow([ts.isoformat(sep=" "), f"{temp:.1f}", rain])

    with open(out / "road_events.csv", "w", newline="", encoding="utf-8") as fr:
        w = csv.writer(fr)
        w.writerow(["event_id", "event_type", "starts_at", "ends_at", "latitude", "longitude",
                    "municipality_or_area", "description"])
        w.writerow(["r1", "перекрытие", (start + timedelta(days=2, hours=10)).isoformat(sep=" "),
                    (start + timedelta(days=2, hours=16)).isoformat(sep=" "), "55.7505", "37.5925",
                    "район Арбат", "Ремонт покрытия"])
        w.writerow(["r2", "мероприятие", (start + timedelta(days=5, hours=18)).isoformat(sep=" "),
                    (start + timedelta(days=5, hours=23)).isoformat(sep=" "), "", "", "",
                    "Городское мероприятие"])

    return {"segments": len(segments), "occupancy_rows": n_occ, "violations": n_viol, "dir": str(out)}
