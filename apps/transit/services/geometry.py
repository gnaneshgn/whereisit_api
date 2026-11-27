"""
Pure-Python geometry for route shapes (no GIS dependencies).

A ``Polyline`` is prepared once per shape: points are projected to a local
planar frame (equirectangular around the shape's mean latitude, error < 0.1%
over a city or an intercity route) and cumulative distances are precomputed.

Key idea: a position on a route is its **distance along the shape**. Stops get
one at import time; a live bus gets one per GPS fix. Comparing the two answers
"which stop has the bus passed / what is next" without guessing direction.

APSRTC shapes are sparse (median 315 m between points), so projection is onto
*segments*, never onto the nearest vertex.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6371008.8
_DEG = math.pi / 180.0


# ---------------------------------------------------------------------------
# Google encoded polyline (precision 5): ~5 bytes per point instead of ~20.
# ---------------------------------------------------------------------------
def encode_polyline(points, precision: int = 5) -> str:
    factor = 10 ** precision
    out = []
    prev_lat = prev_lon = 0
    for lat, lon in points:
        ilat, ilon = int(round(lat * factor)), int(round(lon * factor))
        for delta in (ilat - prev_lat, ilon - prev_lon):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def decode_polyline(text: str, precision: int = 5) -> list[tuple[float, float]]:
    factor = 10 ** precision
    points = []
    index = lat = lon = 0
    length = len(text)
    while index < length:
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                byte = ord(text[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lon += deltas[1]
        points.append((lat / factor, lon / factor))
    return points


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = lat1 * _DEG, lat2 * _DEG
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin((lon2 - lon1) * _DEG / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, a)))


def bearing_difference(a: float, b: float) -> float:
    """Smallest absolute difference between two compass bearings (0..180)."""
    diff = abs(a - b) % 360.0
    return 360.0 - diff if diff > 180.0 else diff


@dataclass(frozen=True)
class Projection:
    along_m: float       # distance along the polyline to the snapped point
    offset_m: float      # perpendicular distance from the input point to the polyline
    segment: int         # index of the segment the point snapped to
    bearing: float       # compass bearing of that segment (direction of travel)


class Polyline:
    def __init__(self, points):
        pts = []
        for lat, lon in points:
            if not pts or (lat, lon) != pts[-1]:
                pts.append((float(lat), float(lon)))
        if not pts:
            raise ValueError("polyline needs at least one point")
        if len(pts) == 1:
            pts.append(pts[0])
        self.points = pts
        mean_lat = sum(p[0] for p in pts) / len(pts)
        self._ky = EARTH_RADIUS_M * _DEG
        self._kx = self._ky * math.cos(mean_lat * _DEG)
        self._xy = [self._to_xy(lat, lon) for lat, lon in pts]
        cum = [0.0]
        for (x1, y1), (x2, y2) in zip(self._xy, self._xy[1:]):
            cum.append(cum[-1] + math.hypot(x2 - x1, y2 - y1))
        self.cum = cum
        self.length_m = cum[-1]
        self._bearings = [
            (math.degrees(math.atan2(x2 - x1, y2 - y1)) + 360.0) % 360.0
            for (x1, y1), (x2, y2) in zip(self._xy, self._xy[1:])
        ]

    def _to_xy(self, lat: float, lon: float):
        return lon * self._kx, lat * self._ky

    def _project_segment(self, i: int, px: float, py: float):
        (x1, y1), (x2, y2) = self._xy[i], self._xy[i + 1]
        dx, dy = x2 - x1, y2 - y1
        seg_len_sq = dx * dx + dy * dy
        t = 0.0 if seg_len_sq == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg_len_sq))
        cx, cy = x1 + t * dx, y1 + t * dy
        return math.hypot(px - cx, py - cy), self.cum[i] + t * (self.cum[i + 1] - self.cum[i])

    def candidates(self, lat: float, lon: float, max_offset_m: float) -> list[Projection]:
        """
        Every distinct place where the polyline passes within ``max_offset_m``:
        one best projection per run of consecutive nearby segments. A route that
        loops or runs out-and-back along the same road yields several candidates;
        callers choose between them with bearing / previous position / schedule.
        """
        px, py = self._to_xy(lat, lon)
        result: list[Projection] = []
        best = None
        last_i = -2
        last_along = None
        # Adjacent segments of one pass project the point close together; a jump in
        # along-distance means the path folded back (U-turn) and this is another pass.
        jump = 2.0 * max_offset_m + 1.0
        for i in range(len(self._xy) - 1):
            offset, along = self._project_segment(i, px, py)
            if offset > max_offset_m:
                continue
            new_pass = i != last_i + 1 or (last_along is not None and along - last_along > jump)
            if new_pass and best is not None:
                result.append(best)
                best = None
            if best is None or offset < best.offset_m:
                best = Projection(along, offset, i, self._bearings[i])
            last_i = i
            last_along = along
        if best is not None:
            result.append(best)
        return result

    def nearest(self, lat: float, lon: float, min_along_m: float = 0.0) -> Projection:
        """Closest projection whose along-distance is at least ``min_along_m``."""
        px, py = self._to_xy(lat, lon)
        best = None
        for i in range(len(self._xy) - 1):
            if self.cum[i + 1] < min_along_m:
                continue
            offset, along = self._project_segment(i, px, py)
            if along < min_along_m:
                continue
            if best is None or offset < best.offset_m:
                best = Projection(along, offset, i, self._bearings[i])
        if best is None:  # min_along beyond the end: snap to the end
            last = len(self._xy) - 2
            offset, along = self._project_segment(last, px, py)
            best = Projection(along, offset, last, self._bearings[last])
        return best

    def point_at(self, along_m: float) -> tuple[float, float]:
        along_m = max(0.0, min(self.length_m, along_m))
        lo, hi = 0, len(self.cum) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if self.cum[mid] <= along_m:
                lo = mid
            else:
                hi = mid
        span = self.cum[hi] - self.cum[lo]
        t = 0.0 if span == 0 else (along_m - self.cum[lo]) / span
        (lat1, lon1), (lat2, lon2) = self.points[lo], self.points[hi]
        return lat1 + t * (lat2 - lat1), lon1 + t * (lon2 - lon1)

    def slice(self, start_m: float, end_m: float) -> list[tuple[float, float]]:
        """The part of the polyline between two along-distances (inclusive end points)."""
        start_m = max(0.0, min(self.length_m, start_m))
        end_m = max(start_m, min(self.length_m, end_m))
        out = [self.point_at(start_m)]
        out.extend(p for p, d in zip(self.points, self.cum) if start_m < d < end_m)
        out.append(self.point_at(end_m))
        return out


def locate_stops(polyline: Polyline, stops, max_offset_m: float = 150.0, tie_m: float = 40.0):
    """
    Distance along ``polyline`` for each stop, in trip order.

    Stops are visited in order, so each one is searched only *ahead* of the
    previous stop; among the nearby candidates the earliest good one wins
    (handles loops and out-and-back roads). Distances never decrease.
    Returns [(along_m, offset_m)].
    """
    result = []
    prev = 0.0
    for lat, lon in stops:
        ahead = [c for c in polyline.candidates(lat, lon, max_offset_m) if c.along_m >= prev - 30.0]
        if ahead:
            best_offset = min(c.offset_m for c in ahead)
            chosen = min((c for c in ahead if c.offset_m <= best_offset + tie_m), key=lambda c: c.along_m)
        else:
            chosen = polyline.nearest(lat, lon, min_along_m=prev)
        along = max(prev, chosen.along_m)
        result.append((along, chosen.offset_m))
        prev = along
    return result
