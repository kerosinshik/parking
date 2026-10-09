import json
from datetime import date, datetime

import duckdb
import pytest

from parkan.track import torgi
from parkan.track.store import write_events


def flatten(obj):
    """Сериализует объект так же, как страница torgi.mos.ru: плоский массив со ссылками на индексы."""
    out = []

    def put(v):
        i = len(out)
        out.append(None)
        if isinstance(v, dict):
            out[i] = {k: put(x) for k, x in v.items()}
        elif isinstance(v, list):
            out[i] = [put(x) for x in v]
        else:
            out[i] = v
        return i

    root = put(None)
    out[root] = ["ShallowReactive", put({"data": {"$fabc": obj}, "state": {}})]
    return out


def page(status="Признаны состоявшимися", final="727700,00", lat=55.8, subway=None):
    lot = {
        "tenderId": 16909867,
        "sidebar": {"startPrice": "383 000,00 руб.", "tenderStatusInfo": {"statusText": status}},
        "procedureInfo": [
            {"label": "Начальная цена за объект", "value": "383 000,00 руб."},
            {"label": "Размер задатка", "value": "76 600,00 руб."},
            {"label": "Шаг аукциона", "value": "19 150,00 руб."},
            {"label": "Форма проведения", "value": "Открытый аукцион в электронной форме"},
            {"label": "Подведение итогов", "value": "09.08.2016 17:00"},
            {"label": "Итоговая цена", "value": final},
        ],
        "objectInfo": [{"label": "Этаж", "value": "-1"}, {"label": "Кадастровый номер", "value": "77:01:1"}],
        "headerInfo": {"displayAddress": "САО, Башиловская ул., д. 23б, м/м 71",
                       "subway": subway or [{"subwayStationName": "Савёловская", "walkingTime": 9,
                                             "distanceToObject": 0.7}]},
        "mapInfo": {"coords": {"lat": lat, "long": 37.57}},
        "documentInfo": {"documentGroups": [{"groupType": "TradeDocs", "files": [
            {"name": "Протокол аукциона", "downloadLink": "https://api.torgi.mos.ru/doc?id=1"}]}]},
    }
    return f'<html><script type="application/json" id="__NUXT_DATA__">{json.dumps(flatten(lot))}</script></html>'


def test_parse_page_reads_result_location_and_protocol():
    info = torgi.parse_page(page())
    assert info["status"] == "Признаны состоявшимися"
    assert info["final_price"] == 727700.0
    assert info["start_price_page"] == 383000.0
    assert info["deposit"] == 76600.0
    assert info["lat"] == 55.8 and info["lon"] == 37.57
    assert info["subway"] == "Савёловская (9 мин пешком, 0.7 км)"
    assert info["floor"] == "-1"
    assert info["auction_protocol_url"] == "https://api.torgi.mos.ru/doc?id=1"


def test_parse_page_zero_final_price_means_no_result():
    assert torgi.parse_page(page(status="Прием заявок завершен", final="0"))["final_price"] is None


def test_parse_page_without_data_fails_clearly():
    with pytest.raises(torgi.PageError):
        torgi.parse_page("<html>404</html>")


def lot_event(tid, trades, stage="снято с публикации", kind="машино-место"):
    payload = {"ID": tid, "ObjectType": kind, "Stage": stage, "TradesDate": trades, "AdmArea": "Северный административный округ",
               "District": "Савёловский район", "Address": "Российская Федерация, город Москва, внутригородская "
               "территория муниципальный округ Савёловский, Башиловская улица, дом 23б",
               "Space": "13.2", "StartPrice": "383000", "Coordinates": "55.80,37.57",
               "WebSite": [{"WebSite": f"investmoscow.ru/tenders/tender/{tid}"}]}
    return (1461, str(tid), datetime(2026, 10, 9, 6, 0), "added", "h", None, json.dumps(payload, ensure_ascii=False), None)


@pytest.fixture
def state(tmp_path):
    con = duckdb.connect(":memory:")
    write_events(con, tmp_path / "state", 1461, datetime(2026, 10, 9, 6, 0), [
        lot_event(1, "08.10.2026"),                      # торги прошли — новый
        lot_event(2, "20.10.2026", stage="опубликовано"),  # торги впереди
        lot_event(3, "07.10.2026"),                      # итог уже в таблице
        lot_event(4, "06.10.2026"),                      # итог ещё не опубликован — перепроверить
        lot_event(5, "08.10.2026", kind="квартира"),     # не машино-место
        lot_event(6, "01.01.2026"),                      # старше окна --days
    ])
    yield con, tmp_path
    con.close()


def test_collect_appends_new_and_rechecks_pending(state):
    con, tmp = state
    results = tmp / "results.csv"
    torgi.save(results, {
        "3": {"tender_id": "3", "trades_date": "2026-10-07", "status": "Признаны несостоявшимися", "checked_at": "2026-10-08 10:00"},
        "4": {"tender_id": "4", "trades_date": "2026-10-06", "status": "Прием заявок завершен", "checked_at": "2026-10-08 10:00"},
    })
    pages = {"1": page(), "4": page(status="Единственный участник", final="383000,00")}
    seen = []

    def transport(url):
        tid = url.rsplit("/", 1)[1]
        seen.append(tid)
        return (200, pages[tid]) if tid in pages else (404, "")

    stats = torgi.collect(con, tmp / "state", results, date(2026, 10, 9), date(2026, 9, 9),
                          pause=0, transport=transport, sleep=lambda s: None, now=datetime(2026, 10, 9, 7, 0), log=lambda *a: None)
    assert sorted(seen) == ["1", "4"]
    assert stats["new"] == 1 and stats["rechecked"] == 1 and stats["sold"] == 2 and stats["errors"] == 0
    rows = torgi.load(results)
    assert set(rows) == {"1", "3", "4"}
    assert rows["1"]["final_price"] == "727700.0" and rows["1"]["final_to_start"] == "1.9"
    assert rows["1"]["url"] == "https://torgi.mos.ru/tender/1"
    assert rows["4"]["status"] == "Единственный участник" and rows["4"]["checked_at"] == "2026-10-09 07:00"


def test_collect_marks_missing_pages_and_keeps_going_after_errors(state):
    con, tmp = state
    results = tmp / "results.csv"

    def transport(url):
        if url.endswith("/1"):
            raise ConnectionError("сброс соединения")
        return 404, ""

    stats = torgi.collect(con, tmp / "state", results, date(2026, 10, 9), date(2026, 9, 9),
                          pause=0, transport=transport, sleep=lambda s: None, log=lambda *a: None)
    rows = torgi.load(results)
    assert stats["errors"] == 1
    assert "1" not in rows                     # сбой сети — попробуем в следующий раз
    assert rows["4"]["status"] == torgi.MISSING and rows["3"]["status"] == torgi.MISSING


def test_export_slim_shortens_fields(state, tmp_path):
    con, tmp = state
    results = tmp / "results.csv"
    torgi.collect(con, tmp / "state", results, date(2026, 10, 9), date(2026, 9, 9), pause=0,
                  transport=lambda url: (200, page()), sleep=lambda s: None, log=lambda *a: None)
    out = tmp / "slim.csv"
    assert torgi.export_slim(results, out) == 3
    import csv
    r = next(csv.DictReader(open(out, encoding="utf-8")))
    assert r["okrug"] == "САО" and r["district"] == "Савёловский район"
    assert r["house"] == "Башиловская улица, дом 23б"
    assert r["subway"] == "Савёловская (9 мин пешком, 0.7 км)"
    assert r["final_price"] == "727700"
