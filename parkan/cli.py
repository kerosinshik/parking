"""Командная строка: parkan <команда> [...]. Подробности — parkan <команда> --help."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import duckdb

from . import db, demo
from .mart import MartParams, area_summary, build_mart, export_parquet
from .raw import store_bytes, store_file
from .report import render
from .sources import SOURCE_TYPES, context, mosdata, occupancy, violations
from .track.commands import register as register_track


def _print_rejected(rejected, limit=10):
    for line, reason in rejected[:limit]:
        print(f"  строка {line}: {reason}", file=sys.stderr)
    if len(rejected) > limit:
        print(f"  ... и ещё {len(rejected) - limit}", file=sys.stderr)


def _mart_params(a) -> MartParams:
    return MartParams(window_min=a.window, max_distance_m=a.max_distance, near_full=a.near_full,
                      peak_lookback_min=a.peak_lookback, include_rejected=a.include_rejected)


def cmd_init(a, con):
    print(f"База готова: {a.db}")


def cmd_fetch_623(a, con):
    client = mosdata.MosDataClient(os.environ.get("MOS_API_KEY", ""), page_size=a.page_size)
    version = client.version(a.dataset)
    rows = list(client.iter_rows(a.dataset) if a.no_geometry else client.iter_features(a.dataset))
    payload = json.dumps({"version": version, "rows": rows}, ensure_ascii=False).encode()
    path, new = store_bytes(con, f"mos_{a.dataset}", payload, suffix=".json", raw_dir=a.raw_dir,
                            meta={"version": version})
    print(f"Снимок {'сохранён' if new else 'не изменился'}: {path}")
    stats = mosdata.load_segments(con, rows, mosdata.version_label(version))
    _report_segments(stats)


def cmd_load_623(a, con):
    store_file(con, f"mos_{a.dataset}", a.file, raw_dir=a.raw_dir)
    rows, version = mosdata.rows_from_file_payload(json.loads(Path(a.file).read_text(encoding="utf-8-sig")))
    stats = mosdata.load_segments(con, rows, a.version or version or "file")
    _report_segments(stats)


def _report_segments(stats):
    print(f"Сегментов: {stats['rows']} (новых {stats['added']}, изменённых {stats['modified']}, "
          f"удалённых {stats['removed']}, без изменений {stats['unchanged']})")
    if stats["errors"]:
        print(f"Пропущено строк: {len(stats['errors'])}", file=sys.stderr)
        _print_rejected(stats["errors"])


def cmd_import_violations(a, con):
    store_file(con, "violations", a.file, raw_dir=a.raw_dir)
    try:
        r = violations.import_violations(con, a.file, drop_pii=a.drop_pii, strict=a.strict)
    except violations.PiiColumnsError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
    if r.dropped_columns:
        print(f"Отброшены поля с персональными данными: {', '.join(r.dropped_columns)}")
    print(f"Загружено нарушений: {r.inserted}, отклонено строк: {len(r.rejected)}")
    _print_rejected(r.rejected)


def cmd_import_occupancy(a, con):
    store_file(con, f"occupancy_{a.source_name or Path(a.file).stem}", a.file, raw_dir=a.raw_dir)
    r = occupancy.import_occupancy_file(con, a.file, source_type=a.source_type, source_name=a.source_name,
                                        mapping=occupancy.parse_mapping(a.map))
    print(f"Загружено снимков: {r.inserted}, отклонено: {len(r.rejected)}")
    _print_rejected(r.rejected)


def cmd_collect_occupancy(a, con):
    src = occupancy.HttpJsonOccupancySource(
        a.url, source_name=a.source_name, mapping=occupancy.parse_mapping(a.map),
        headers=occupancy.parse_headers(os.environ.get("OCCUPANCY_HEADERS")), records_key=a.records_key)
    r = src.collect(con, raw_dir=a.raw_dir)
    print(f"Загружено снимков: {r.inserted}, отклонено: {len(r.rejected)}")
    _print_rejected(r.rejected)


def cmd_import_road_events(a, con):
    n, rejected = context.import_road_events(con, a.file, a.source_name)
    print(f"Загружено дорожных событий: {n}, отклонено: {len(rejected)}")
    _print_rejected(rejected)


def cmd_import_weather(a, con):
    n, rejected = context.import_weather(con, a.file, a.source_name)
    print(f"Загружено часов погоды: {n}, отклонено: {len(rejected)}")
    _print_rejected(rejected)


def cmd_build_mart(a, con):
    s = build_mart(con, _mart_params(a))
    print(f"Сопоставлено по street_segment_id: {s['matched_by_segment_id']}, по координате: "
          f"{s['matched_nearest']}, не сопоставлено: {s['unmatched']}")
    print(f"Со снимком загрузки в окне ±{a.window} мин: {s['with_occupancy']}")
    print(f"Строк почасовой витрины: {s['hourly_rows']}")


def cmd_summary(a, con):
    rows = area_summary(con, _mart_params(a), a.area)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1, default=str))
        return
    for r in rows:
        f = lambda v, nd=2: "—" if v is None else f"{v:.{nd}f}"
        print(f"\n{r['area']}")
        print(f"  нарушений: {r['violations']} за {r['days']} дн. ({f(r['violations_per_day'], 1)} в сутки)")
        print(f"  на 100 мест в сутки: {f(r['violations_per_100_spaces_per_day'])}; "
              f"на 1 км кромки в сутки: {f(r['violations_per_km_per_day'])}")
        print(f"  средняя загрузка: {f(r['mean_occupancy_rate'])}; доля нарушений при высокой загрузке: "
              f"{f(r['near_full_share'])}")
        print(f"  корреляция загрузка~нарушения (по часам): {f(r['corr_occupancy_violations'])}; "
              f"осадки~нарушения: {f(r['corr_precipitation_violations'])}")
        print(f"  медиана минут от начала пика загрузки до нарушения: "
              f"{f(r['median_minutes_from_peak_start'], 0)}")


def cmd_report(a, con):
    print(f"Отчёт: {render(con, a.out, _mart_params(a), a.title, fragment=getattr(a, 'fragment', False))}")


def cmd_export(a, con):
    for p in export_parquet(con, a.out):
        print(p)


def cmd_demo(a, con):
    work = Path(a.workdir)
    info = demo.generate(work / "input", days=a.days, seed=a.seed)
    print(f"Сгенерировано: {info['segments']} сегментов, {info['occupancy_rows']} снимков, "
          f"{info['violations']} нарушений -> {info['dir']}")
    a.raw_dir = work / "raw"
    for fn, args in [
        (cmd_load_623, {"file": work / "input/mos_623_snapshot.json", "dataset": 623, "version": None}),
        (cmd_import_occupancy, {"file": work / "input/occupancy.csv", "source_type": "manual_import",
                                "source_name": "demo", "map": None}),
        (cmd_import_violations, {"file": work / "input/violations.csv", "drop_pii": False, "strict": True}),
        (cmd_import_weather, {"file": work / "input/weather.csv", "source_name": "demo"}),
        (cmd_import_road_events, {"file": work / "input/road_events.csv", "source_name": "demo"}),
        (cmd_build_mart, {}),
        (cmd_summary, {"area": None, "json": False}),
        (cmd_report, {"out": work / "report.html", "title": "Парковки и нарушения — демо"}),
    ]:
        rc = fn(argparse.Namespace(**{**vars(a), **args}), con)
        if rc:
            return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="parkan", description="Аналитика загрузки парковок и нарушений (Москва)")
    p.add_argument("--db", default=str(db.default_db_path()), help="файл DuckDB (по умолчанию $PARKAN_DB или data/parkan.duckdb)")
    p.add_argument("--raw-dir", default=None, help="каталог raw-снимков (по умолчанию $PARKAN_RAW_DIR или data/raw)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def mart_opts(sp):
        sp.add_argument("--window", type=int, default=15, help="окно поиска снимка загрузки, ± минут")
        sp.add_argument("--max-distance", type=float, default=100.0, help="макс. расстояние до сегмента, м")
        sp.add_argument("--near-full", type=float, default=0.9, help="порог высокой загрузки (0–1)")
        sp.add_argument("--peak-lookback", type=int, default=180, help="глубина поиска начала пика, минут")
        sp.add_argument("--include-rejected", action="store_true", help="считать отклонённые сообщения нарушениями")

    sub.add_parser("init", help="создать базу").set_defaults(fn=cmd_init)

    sp = sub.add_parser("fetch-623", help="скачать набор № 623 через apidata.mos.ru (нужен MOS_API_KEY)")
    sp.add_argument("--dataset", type=int, default=mosdata.PARKING_DATASET_ID)
    sp.add_argument("--page-size", type=int, default=1000)
    sp.add_argument("--no-geometry", action="store_true", help="брать /rows вместо /features (без координат)")
    sp.set_defaults(fn=cmd_fetch_623)

    sp = sub.add_parser("load-623", help="загрузить сохранённый снимок/выгрузку набора № 623 (JSON)")
    sp.add_argument("file")
    sp.add_argument("--dataset", type=int, default=mosdata.PARKING_DATASET_ID)
    sp.add_argument("--version", help="метка версии, если её нет в файле")
    sp.set_defaults(fn=cmd_load_623)

    sp = sub.add_parser("import-violations", help="импорт обезличенных нарушений (CSV/JSON)")
    sp.add_argument("file")
    sp.add_argument("--drop-pii", action="store_true", help="отбросить поля с персональными данными вместо отказа")
    sp.add_argument("--strict", action="store_true", help="прервать импорт при любой ошибочной строке")
    sp.set_defaults(fn=cmd_import_violations)

    sp = sub.add_parser("import-occupancy", help="импорт снимков загрузки из файла (CSV/JSON)")
    sp.add_argument("file")
    sp.add_argument("--source-type", choices=SOURCE_TYPES, default="manual_import")
    sp.add_argument("--source-name")
    sp.add_argument("--map", help='соответствие полей, напр. "parking_id=id,free_spaces=free"')
    sp.set_defaults(fn=cmd_import_occupancy)

    sp = sub.add_parser("collect-occupancy", help="опросить официальный JSON-endpoint загрузки (official_api)")
    sp.add_argument("--url", required=True)
    sp.add_argument("--source-name", required=True)
    sp.add_argument("--map")
    sp.add_argument("--records-key", help="ключ списка записей в ответе")
    sp.set_defaults(fn=cmd_collect_occupancy)

    for name, fn, hlp in [("import-road-events", cmd_import_road_events, "импорт дорожных событий/перекрытий"),
                          ("import-weather", cmd_import_weather, "импорт почасовой погоды")]:
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("file")
        sp.add_argument("--source-name", default="manual")
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("build-mart", help="сопоставить нарушения с парковками и загрузкой, собрать витрину")
    mart_opts(sp)
    sp.set_defaults(fn=cmd_build_mart)

    sp = sub.add_parser("summary", help="сводка по районам")
    sp.add_argument("--area")
    sp.add_argument("--json", action="store_true")
    mart_opts(sp)
    sp.set_defaults(fn=cmd_summary)

    sp = sub.add_parser("report", help="HTML-дашборд")
    sp.add_argument("--out", default="data/report.html")
    sp.add_argument("--title", default="Парковки Москвы")
    sp.add_argument("--fragment", action="store_true", help="без обёртки html/head/body (для хостинга страниц)")
    mart_opts(sp)
    sp.set_defaults(fn=cmd_report)

    sp = sub.add_parser("export", help="выгрузить нормализованные таблицы и витрину в Parquet")
    sp.add_argument("--out", default="data/export")
    sp.set_defaults(fn=cmd_export)

    register_track(sub)

    sp = sub.add_parser("demo", help="сгенерировать синтетические данные и прогнать весь конвейер")
    sp.add_argument("--workdir", default="demo_out")
    sp.add_argument("--days", type=int, default=14)
    sp.add_argument("--seed", type=int, default=42)
    mart_opts(sp)
    sp.set_defaults(fn=cmd_demo)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "demo" and a.db == str(db.default_db_path()):
        a.db = str(Path(a.workdir) / "parkan.duckdb")
    # учёт динамических данных работает на журнале в Parquet, своя база ему не нужна
    con = duckdb.connect(":memory:") if getattr(a, "track", False) else db.connect(a.db)
    try:
        return a.fn(a, con) or 0
    except Exception as e:  # понятное сообщение вместо трассировки для пользователя
        if os.environ.get("PARKAN_DEBUG"):
            raise
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
