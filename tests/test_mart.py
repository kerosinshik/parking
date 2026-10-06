from datetime import datetime

import pytest

from parkan.mart import MartParams, area_summary, build_mart, export_parquet, pearson
from parkan.sources import mosdata, occupancy, violations

from .conftest import segment_row, write_csv

T = "2026-09-07 "


@pytest.fixture
def scenario(con, tmp_path):
    mosdata.load_segments(con, [segment_row(1, 55.75, 37.6, cap=10, area="район А")], "1")
    occupancy.import_occupancy_file(con, write_csv(
        tmp_path / "o.csv", ["parking_id", "measured_at", "occupied_spaces"],
        [[1, T + "11:30", 5], [1, T + "11:45", 9], [1, T + "12:00", 10], [1, T + "12:20", 6]]))
    violations.import_violations(con, write_csv(
        tmp_path / "v.csv",
        ["violation_id", "source_system", "violation_type", "status", "occurred_at", "latitude", "longitude",
         "street_segment_id", "municipality_or_area"],
        [["v1", "МАДИ", "тротуар", "постановление", T + "12:05", "55.7501", "37.6", "", ""],
         ["v2", "МАДИ", "тротуар", "проверено", T + "12:18", "55.7501", "37.6", "", ""],
         ["v3", "МАДИ", "тротуар", "отклонено", T + "13:00", "", "", "1", ""],
         ["v4", "МАДИ", "тротуар", "проверено", T + "12:10", "55.80", "37.6", "", ""],
         ["v5", "МАДИ", "тротуар", "проверено", T + "12:10", "55.80", "37.6", "", "район Б"]]))
    return con


def test_matching_and_occupancy_join(scenario):
    con = scenario
    stats = build_mart(con)
    assert (stats["matched_by_segment_id"], stats["matched_nearest"], stats["unmatched"]) == (1, 2, 2)
    rows = {r[0]: r[1:] for r in con.execute(
        "SELECT violation_id, parking_id, match_method, area, snapshot_at, occupancy_rate, minutes_since_peak_start "
        "FROM violation_occupancy").fetchall()}
    # v1: снимок ДО события (12:00, 5 мин) предпочтён снимку после (12:20); пик начался в 11:45
    assert rows["v1"] == ("1", "nearest", "район А", datetime(2026, 9, 7, 12, 0), 1.0, 20.0)
    # v2: снимок до (12:00) за пределами окна 15 мин — берётся снимок после (12:20)
    assert rows["v2"][3:5] == (datetime(2026, 9, 7, 12, 20), 0.6)
    assert rows["v2"][5] == 33.0
    # v3: только идентификатор сегмента; снимков в окне нет
    assert rows["v3"][:2] == ("1", "segment_id") and rows["v3"][4] is None
    assert rows["v4"] == (None, "none", None, None, None, None)
    assert rows["v5"][2] == "район Б"


def test_hourly_metrics(scenario):
    con = scenario
    build_mart(con)
    h = {(r[0], r[1].hour): r[2:] for r in con.execute("""
        SELECT area, hour_start, occupancy_observed, violations, n_rejected, occupancy_rate,
               violations_per_100_spaces, violations_per_100_occupied_spaces,
               share_of_violations_near_full_occupancy, violations_per_km_of_parking_edge
        FROM hourly_area_metrics""").fetchall()}
    assert h[("район А", 11)][:3] == (True, 0, 0) and h[("район А", 11)][3] == pytest.approx(0.7)
    obs, n, rej, rate, per100, per100occ, near_full, per_km = h[("район А", 12)]
    assert (obs, n, rej) == (True, 2, 0)
    assert rate == pytest.approx(0.8) and per100 == pytest.approx(20) and per100occ == pytest.approx(25)
    assert near_full == pytest.approx(0.5)
    assert per_km == pytest.approx(2 / 0.0627, rel=0.02)
    assert h[("район А", 13)][:3] == (False, 0, 1)        # отклонённое не считается нарушением
    assert h[("район Б", 12)][:2] == (False, 1)

    build_mart(con, MartParams(include_rejected=True))
    assert con.execute("SELECT violations FROM hourly_area_metrics WHERE area='район А' AND hour(hour_start)=13"
                       ).fetchone()[0] == 1


def test_window_and_distance_params(scenario):
    con = scenario
    build_mart(con, MartParams(window_min=1, max_distance_m=5))
    assert con.execute("SELECT count(*) FROM violation_matches WHERE match_method='nearest'").fetchone()[0] == 0
    build_mart(con, MartParams(window_min=1))
    assert con.execute("SELECT count(occupancy_rate) FROM violation_occupancy").fetchone()[0] == 0


def test_summary_and_export(scenario, tmp_path):
    con = scenario
    build_mart(con)
    s = {r["area"]: r for r in area_summary(con)}
    a = s["район А"]
    assert a["violations"] == 2 and a["with_occupancy"] == 2 and a["near_full_share"] == 0.5
    assert a["median_minutes_from_peak_start"] == pytest.approx(26.5)
    assert a["violations_per_100_spaces_per_day"] == pytest.approx(20)
    files = export_parquet(con, tmp_path / "out")
    assert any(f.endswith("mart/hourly_area_metrics.parquet") for f in files)
    assert con.execute(f"SELECT count(*) FROM '{tmp_path}/out/normalized/violation_events.parquet'").fetchone()[0] == 5


def test_pearson():
    assert pearson([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1)
    assert pearson([1, 2, 3], [3, 2, 1]) == pytest.approx(-1)
    assert pearson([1, 1, 1], [1, 2, 3]) is None
    assert pearson([1, None], [1, 2]) is None
