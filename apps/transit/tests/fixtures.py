"""
A tiny GTFS network with known geometry, written to a temp folder.

Road: straight east-west line at latitude 17.70 starting at lon 83.20.
Stops S1..S4 sit 15 m north of it, exactly 1 km apart (S1 = 0 km ... S4 = 3 km).

  route A009 (city):  pattern EAST  S1 -> S4 on shape SH_E   trips T_EAST (08:00), T_EAST2 (09:00)
                      pattern WEST  S4 -> S1 on shape SH_W   trip  T_WEST (08:30)
  route X100 (intercity): S1 -> S3, shape missing -> approximate path       trip T_NOSHAPE

Every leg takes 5 minutes. T_EAST2 has no time at S3 (must be interpolated).
"""
import csv
import math
import tempfile
from pathlib import Path

LAT = 17.70
LON0 = 83.20
KM_LON = 1.0 / (6371.0088 * math.pi / 180.0 * math.cos(math.radians(LAT)))  # degrees of longitude per km
STOP_LAT = LAT + 15 / 111195.0  # 15 m north of the road


def lon_at(km):
    return round(LON0 + km * KM_LON, 7)


STOPS = {f"S{i + 1}": (f"Stop {i + 1}", STOP_LAT, lon_at(i)) for i in range(4)}


def _write(directory: Path, name: str, header, rows):
    with (directory / f"{name}.txt").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def _times(start_h, start_m, n, skip=None):
    out = []
    for i in range(n):
        total = start_h * 60 + start_m + 5 * i
        out.append("" if i == skip else f"{total // 60:02d}:{total % 60:02d}:00")
    return out


def write_network(directory: Path):
    _write(directory, "agency", ["agency_id", "agency_name", "agency_url", "agency_timezone"],
           [["APSRTC", "Test RTC", "https://example.invalid", "Asia/Kolkata"]])
    _write(directory, "feed_info", ["feed_publisher_name", "feed_publisher_url", "feed_lang", "feed_version"],
           [["tests", "https://example.invalid", "en", "test-1"]])
    _write(directory, "calendar", ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday",
                                   "saturday", "sunday", "start_date", "end_date"],
           [["daily", 1, 1, 1, 1, 1, 1, 1, "20261101", "20271031"]])
    _write(directory, "stops", ["stop_id", "stop_name", "stop_lat", "stop_lon"],
           [[sid, name, lat, lon] for sid, (name, lat, lon) in STOPS.items()])
    _write(directory, "routes", ["route_id", "agency_id", "route_short_name", "route_long_name", "route_desc", "route_type"], [
        ["a009-stop-1-stop-4", "APSRTC", "A009", "Stop 1 – Stop 4", "City · City Ordinary · 3 trips/day", 3],
        ["x100-stop-1-stop-3", "APSRTC", "X100", "Stop 1 – Stop 3", "Intercity · Express · 1 trips/day", 3],
    ])
    _write(directory, "trips", ["route_id", "service_id", "trip_id", "trip_short_name", "trip_headsign", "shape_id", "trip_type"], [
        ["a009-stop-1-stop-4", "daily", "T_EAST", "1", "Stop 4", "SH_E", "City Ordinary"],
        ["a009-stop-1-stop-4", "daily", "T_EAST2", "2", "Stop 4", "SH_E", "City Ordinary"],
        # Misleading headsign on purpose (as ~24% of the real feed): must never become the destination
        ["a009-stop-1-stop-4", "daily", "T_WEST", "3", "Far Town", "SH_W", "City Ordinary"],
        ["x100-stop-1-stop-3", "daily", "T_NOSHAPE", "4", "Stop 3", "MISSING", "Express"],
    ])
    rows = []
    for trip, stops, times in (
        ("T_EAST", ["S1", "S2", "S3", "S4"], _times(8, 0, 4)),
        ("T_EAST2", ["S1", "S2", "S3", "S4"], _times(9, 0, 4, skip=2)),
        ("T_WEST", ["S4", "S3", "S2", "S1"], _times(8, 30, 4)),
        ("T_NOSHAPE", ["S1", "S3"], _times(10, 0, 2)),
    ):
        for seq, (sid, t) in enumerate(zip(stops, times), start=1):
            rows.append([trip, t, t, sid, seq, 1])
    _write(directory, "stop_times", ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "timepoint"], rows)
    shape_rows = []
    points = [(LAT, lon_at(k / 2)) for k in range(7)]  # every 500 m, 0..3 km
    for sid, pts in (("SH_E", points), ("SH_W", list(reversed(points)))):
        for seq, (lat, lon) in enumerate(pts):
            shape_rows.append([sid, lat, lon, seq])
    _write(directory, "shapes", ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"], shape_rows)


class GtfsFolder:
    """Context manager yielding a temp folder containing the test network."""

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        path = Path(self._tmp.name)
        write_network(path)
        return path

    def __exit__(self, *exc):
        self._tmp.cleanup()
