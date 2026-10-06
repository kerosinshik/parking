import os

import pytest

from parkan.geo import NearestIndex, centroid, edge_length_m, haversine_m
from parkan.raw import store_bytes


def test_haversine_known_distance():
    # 0.001° широты ≈ 111 м
    assert haversine_m(55.75, 37.6, 55.751, 37.6) == pytest.approx(111.2, abs=0.5)


def test_edge_length_line_and_polygon():
    line = {"type": "LineString", "coordinates": [[37.6, 55.75], [37.6, 55.751]]}
    assert edge_length_m(line) == pytest.approx(111.2, abs=0.5)
    # прямоугольник 111 м × ~6 м: полупериметр ≈ длина вдоль бордюра + ширина
    poly = {"type": "Polygon", "coordinates": [[[37.6, 55.75], [37.6, 55.751], [37.6001, 55.751],
                                               [37.6001, 55.75], [37.6, 55.75]]]}
    assert edge_length_m(poly) == pytest.approx(111.2 + 6.3, abs=1)
    assert edge_length_m({"type": "Point", "coordinates": [37.6, 55.75]}) is None
    assert centroid(line) == pytest.approx((55.7505, 37.6))


def test_nearest_index_radius():
    idx = NearestIndex([("a", 55.75, 37.6), ("b", 55.7509, 37.6)])
    key, d = idx.nearest(55.7508, 37.6, 100)
    assert key == "b" and d < 15
    assert idx.nearest(55.76, 37.6, 100) is None          # ~1 км — вне радиуса
    assert idx.nearest(55.7545, 37.6, 600)[0] == "b"       # радиус больше ячейки сетки


def test_raw_store_is_immutable_and_deduplicated(con, tmp_path):
    p1, new1 = store_bytes(con, "src", b"hello", suffix=".txt", raw_dir=tmp_path)
    p2, new2 = store_bytes(con, "src", b"hello", suffix=".txt", raw_dir=tmp_path)
    assert new1 and not new2 and p1 == p2
    assert p1.read_bytes() == b"hello"
    assert not os.access(p1, os.W_OK) or os.geteuid() == 0  # файл только для чтения
    p3, new3 = store_bytes(con, "src", b"world", suffix=".txt", raw_dir=tmp_path)
    assert new3 and p3 != p1
