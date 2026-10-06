"""Геометрия без PostGIS: расстояния, центроиды, длина кромки, ближайший сегмент."""

from __future__ import annotations

import math

EARTH_R = 6_371_000.0


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def _lines(geom: dict) -> list[list[list[float]]]:
    """Все линии геометрии GeoJSON (кольца полигонов тоже) в виде списков точек [lon, lat]."""
    t, c = geom.get("type"), geom.get("coordinates")
    if not c:
        return []
    if t == "Point":
        return [[c]]
    if t in ("LineString", "MultiPoint"):
        return [c]
    if t in ("MultiLineString", "Polygon"):
        return list(c)
    if t == "MultiPolygon":
        return [ring for poly in c for ring in poly]
    return []


def centroid(geom: dict) -> tuple[float, float] | None:
    """Средняя точка вершин (lat, lon). Для узких парковочных полос этого достаточно."""
    pts = [p for line in _lines(geom) for p in line]
    if not pts:
        return None
    return (sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts))


def _line_len(line) -> float:
    return sum(haversine_m(a[1], a[0], b[1], b[0]) for a, b in zip(line, line[1:]))


def edge_length_m(geom: dict) -> float | None:
    """Длина парковочной кромки.

    Для линий — их длина. Для полигонов — половина периметра: парковочный карман —
    вытянутый прямоугольник, и полупериметр ≈ длина вдоль бордюра.
    """
    t = geom.get("type")
    if t in ("LineString", "MultiLineString"):
        return sum(_line_len(line) for line in _lines(geom))
    if t in ("Polygon", "MultiPolygon"):
        polys = [geom["coordinates"]] if t == "Polygon" else geom["coordinates"]
        return sum(_line_len(poly[0]) / 2 for poly in polys if poly)
    return None


class NearestIndex:
    """Сеточный индекс точек для поиска ближайшего сегмента в радиусе."""

    CELL_M = 200.0

    def __init__(self, points):
        """points — итерируемое (key, lat, lon)."""
        self.cells: dict[tuple[int, int], list] = {}
        self._dlat = self.CELL_M / 111_320.0
        self._dlon = self.CELL_M / (111_320.0 * math.cos(math.radians(55.75)))
        for key, lat, lon in points:
            self.cells.setdefault(self._cell(lat, lon), []).append((key, lat, lon))

    def _cell(self, lat, lon):
        return (int(math.floor(lat / self._dlat)), int(math.floor(lon / self._dlon)))

    def nearest(self, lat, lon, max_m: float):
        """(key, distance_m) ближайшей точки не дальше max_m или None."""
        ci, cj = self._cell(lat, lon)
        r = int(math.ceil(max_m / self.CELL_M))
        best = None
        for i in range(ci - r, ci + r + 1):
            for j in range(cj - r, cj + r + 1):
                for key, plat, plon in self.cells.get((i, j), ()):
                    d = haversine_m(lat, lon, plat, plon)
                    if d <= max_m and (best is None or d < best[1]):
                        best = (key, d)
        return best
