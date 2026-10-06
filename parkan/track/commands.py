"""Команды CLI учёта динамических данных: track-sync, track-report, track-status."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb

from ..sources.mosdata import MosDataClient
from ..timeutil import now_msk
from .store import event_files
from .streams import STREAMS, resolve
from .sync import sync_dataset


def default_state_dir() -> str:
    return os.environ.get("PARKAN_STATE", "state")


def _parking_con(path):
    """Справочник парковок (№ 623) для связки с тарифами и работами — если база есть."""
    if path and Path(path).exists():
        return duckdb.connect(str(path), read_only=True)
    return None


def cmd_track_sync(a, con) -> int:
    client = MosDataClient(os.environ.get("MOS_API_KEY", ""), page_size=a.page_size)
    observed_at = now_msk()
    failed = 0
    for stream in resolve(a.streams):
        for ds in stream.datasets:
            print(f"[{stream.name}] № {ds.id} {ds.title}: загрузка…", flush=True)
            try:
                r = sync_dataset(con, client, ds, a.state_dir, observed_at=observed_at,
                                 workers=a.workers, max_pages=a.max_pages)
            except Exception as e:   # один набор не должен срывать остальные
                failed += 1
                print(f"  ошибка: {e}", file=sys.stderr)
                continue
            top = ", ".join(f"{k} ×{v}" for k, v in sorted(r.changed_fields.items(), key=lambda x: -x[1])[:5])
            print(f"  записей {r.fetched} из {r.expected}; добавлено {r.added}, изменено {r.changed}"
                  f"{' (' + top + ')' if top else ''}, удалено {r.removed}"
                  f"{'; журнал: ' + r.file if r.file else '; изменений нет'}")
    return 1 if failed else 0


def cmd_track_report(a, con) -> int:
    from .dashboard import render_dashboard
    pc = _parking_con(a.parking_db)
    try:
        out = render_dashboard(con, a.state_dir, a.out, parking_con=pc, title=a.title, fragment=a.fragment)
    finally:
        if pc is not None:
            pc.close()
    print(f"Дашборд: {out}")
    return 0


def cmd_track_status(a, con) -> int:
    for stream in STREAMS.values():
        for ds in stream.datasets:
            files = event_files(a.state_dir, ds.id)
            size = sum(f.stat().st_size for f in files)
            last = files[-1].stem.removeprefix("events-") if files else "—"
            print(f"{stream.name:13s} № {ds.id:<6d} запусков с изменениями: {len(files):3d}, "
                  f"последний: {last}, объём журнала: {size / 1e6:.1f} МБ")
    return 0


def register(sub) -> None:
    def common(sp):
        sp.add_argument("--state-dir", default=default_state_dir(),
                        help="каталог журнала (по умолчанию $PARKAN_STATE или state)")

    sp = sub.add_parser("track-sync", help="снять снимки наборов и записать изменения в журнал")
    common(sp)
    sp.add_argument("--streams", help=f"потоки через запятую: {', '.join(STREAMS)} (по умолчанию все)")
    sp.add_argument("--workers", type=int, default=4, help="параллельных запросов к API")
    sp.add_argument("--page-size", type=int, default=1000)
    sp.add_argument("--max-pages", type=int, help="только первые N страниц (для проверки; удаления не пишутся)")
    sp.set_defaults(fn=cmd_track_sync, track=True)

    sp = sub.add_parser("track-report", help="дашборд по журналу трёх потоков")
    common(sp)
    sp.add_argument("--out", default="data/track.html")
    sp.add_argument("--parking-db", default=os.environ.get("PARKAN_DB", "data/parkan.duckdb"),
                    help="база со справочником парковок № 623 (для тарифов и перекрытых мест)")
    sp.add_argument("--title", default="Пульс города")
    sp.add_argument("--fragment", action="store_true", help="без обёртки html/head/body")
    sp.set_defaults(fn=cmd_track_report, track=True)

    sp = sub.add_parser("track-status", help="состояние журнала")
    common(sp)
    sp.set_defaults(fn=cmd_track_status, track=True)
