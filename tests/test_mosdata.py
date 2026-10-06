import json
import urllib.parse
from datetime import datetime

import pytest

from parkan.sources import mosdata
from parkan.sources.http import HttpError

from .conftest import segment_row


class FakeApi:
    """Имитация apidata.mos.ru: version, постраничные rows, заданные сбои."""

    def __init__(self, rows, fail_first=0, status_on_fail=503):
        self.rows, self.calls = rows, []
        self.fail_left, self.status_on_fail = fail_first, status_on_fail

    def __call__(self, url, headers=None):
        self.calls.append(url)
        if self.fail_left:
            self.fail_left -= 1
            return self.status_on_fail, b"busy"
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        assert q["api_key"] == "KEY"
        if u.path.endswith("/version"):
            return 200, json.dumps({"VersionNumber": 3, "ReleaseNumber": 77}).encode()
        top, skip = int(q["$top"]), int(q["$skip"])
        return 200, json.dumps(self.rows[skip:skip + top], ensure_ascii=False).encode()


def test_client_paginates_and_reads_version():
    rows = [segment_row(i, 55.75, 37.6) for i in range(5)]
    api = FakeApi(rows)
    c = mosdata.MosDataClient("KEY", transport=api, page_size=2)
    assert mosdata.version_label(c.version()) == "3.77"
    got = list(c.iter_rows())
    assert [r["global_id"] for r in got] == [0, 1, 2, 3, 4]
    assert sum("/rows" in u for u in api.calls) == 3          # 2 + 2 + 1
    assert all(u.startswith("https://apidata.mos.ru/v1/datasets/623/") for u in api.calls)


def test_client_retries_5xx_but_not_403():
    sleeps = []
    api = FakeApi([segment_row(1, 55.75, 37.6)], fail_first=2)
    c = mosdata.MosDataClient("KEY", transport=api, sleep=sleeps.append)
    assert len(list(c.iter_rows())) == 1
    assert sleeps == [2.0, 4.0]

    api = FakeApi([], fail_first=1, status_on_fail=403)
    c = mosdata.MosDataClient("KEY", transport=api, sleep=sleeps.append)
    with pytest.raises(HttpError) as e:
        c.version()
    assert e.value.status == 403 and len(api.calls) == 1


def test_client_requires_key():
    with pytest.raises(ValueError):
        mosdata.MosDataClient("")


def test_normalize_api_row_and_flat_row():
    seg = mosdata.normalize_row(segment_row(7, 55.75, 37.6, cap="25", Tariffs=[{"HourPrice": 380}],
                                            SomethingNew="x"))
    assert seg["parking_id"] == "7" and seg["capacity_total"] == 25
    assert seg["municipality_or_area"] == "район Тест" and seg["administrative_district"] == "ЦАО"
    assert seg["latitude"] == pytest.approx(55.75) and seg["edge_length_m"] > 50
    assert json.loads(seg["price"]) == [{"HourPrice": 380}]
    assert json.loads(seg["extra"]) == {"SomethingNew": "x"}
    flat = mosdata.normalize_row({"global_id": 8, "District": "район Б", "CarCapacity": 4,
                                  "Latitude_WGS84": "55,7", "Longitude_WGS84": "37,5"})
    assert flat["latitude"] == 55.7 and flat["longitude"] == 37.5 and flat["geometry"] is None
    with pytest.raises(ValueError):
        mosdata.normalize_row({"Cells": {"ParkingName": "без id"}})


def test_load_segments_keeps_history(con):
    t1, t2, t3 = datetime(2026, 9, 1), datetime(2026, 9, 2), datetime(2026, 9, 3)
    v1 = [segment_row(1, 55.75, 37.6, cap=10), segment_row(2, 55.76, 37.6, cap=20)]
    s = mosdata.load_segments(con, v1, "1", loaded_at=t1)
    assert (s["added"], s["modified"], s["removed"]) == (2, 0, 0)

    s = mosdata.load_segments(con, v1, "1", loaded_at=t2)       # тот же снимок — без изменений
    assert (s["added"], s["modified"], s["removed"], s["unchanged"]) == (0, 0, 0, 2)

    v2 = [segment_row(1, 55.75, 37.6, cap=12), segment_row(3, 55.77, 37.6)]
    s = mosdata.load_segments(con, v2, "2", loaded_at=t3)
    assert (s["added"], s["modified"], s["removed"]) == (1, 1, 1)

    current = dict(con.execute("SELECT parking_id, capacity_total FROM current_segments").fetchall())
    assert current == {"1": 12, "3": 10}
    hist = con.execute("SELECT capacity_total, valid_from, valid_to FROM parking_segments "
                       "WHERE parking_id = '1' ORDER BY valid_from").fetchall()
    assert hist == [(10, t1, t3), (12, t3, None)]
    changes = con.execute("SELECT parking_id, change_type FROM parking_changes "
                          "WHERE source_version = '2' ORDER BY 1").fetchall()
    assert changes == [("1", "modified"), ("2", "removed"), ("3", "added")]


def test_rows_from_file_payload():
    rows, v = mosdata.rows_from_file_payload({"version": {"VersionNumber": 2}, "rows": [1]})
    assert rows == [1] and v == "2"
    assert mosdata.rows_from_file_payload([1, 2]) == ([1, 2], None)
