"""
GTFS static importer: data/apsrtc/*.txt -> transit tables.

Steps
  1. Read stops, routes, trips, stop_times (times may exceed 24:00:00, per GTFS).
  2. Group trips into *patterns* (route + shape + exact ordered stop list).
  3. For each pattern, locate every stop along the road shape (monotonic
     projection, see geometry.locate_stops). If the shape is missing or does
     not fit the stops (median offset > MAX_SHAPE_FIT_M), the path becomes the
     line joining the stops and the pattern is flagged ``path_is_approximate``.
  4. Store per-trip arrival offsets; missing times are interpolated by distance
     between the nearest timed stops (GTFS non-timepoints), never extrapolated.
  5. Replace all transit rows in one transaction, so the API never sees a
     half-imported feed.
"""
from __future__ import annotations

import csv
import logging
import statistics
import time
from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from django.db import transaction

from apps.transit.models import GtfsFeed, Pattern, PatternStop, Route, ServiceType, Shape, Stop, Trip
from apps.transit.services.geometry import Polyline, encode_polyline, haversine_m, locate_stops

logger = logging.getLogger(__name__)

MAX_SHAPE_FIT_M = 300.0   # median stop-to-shape offset above which the shape is rejected
STOP_SNAP_M = 150.0       # search radius when placing a stop on the shape
BATCH = 2000

csv.field_size_limit(2**31 - 1)


class GtfsImportError(Exception):
    pass


@dataclass
class ImportStats:
    stops: int = 0
    routes: int = 0
    trips: int = 0
    patterns: int = 0
    shapes: int = 0
    approximate_patterns: int = 0
    interpolated_times: int = 0
    skipped_trips: int = 0
    seconds: float = 0.0
    notes: list = field(default_factory=list)


def _rows(directory: Path, name: str, required: bool = True):
    path = directory / f"{name}.txt"
    if not path.exists():
        if required:
            raise GtfsImportError(f"{path} not found")
        return
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            yield {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}


def parse_gtfs_time(value) -> int | None:
    """'25:10:00' -> 90600 seconds after service-day midnight. Empty -> None."""
    if not value:
        return None
    try:
        h, m, s = (int(part) for part in value.split(":"))
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


def _parse_date(value):
    try:
        return datetime.strptime(value, "%Y%m%d").date() if value else None
    except ValueError:
        return None


def _service_type(description: str) -> str:
    head = description.split("·")[0].strip().lower()
    if head == "city":
        return ServiceType.CITY
    if head == "intercity":
        return ServiceType.INTERCITY
    return ServiceType.OTHER


def _split_long_name(long_name: str):
    for sep in (" – ", " - ", " — "):
        if sep in long_name:
            origin, _, destination = long_name.partition(sep)
            return origin.strip(), destination.strip()
    return "", ""


def _interpolate(times: list, dists: list) -> tuple[list, int]:
    """Fill None times linearly by distance between the nearest known neighbours."""
    known = [i for i, t in enumerate(times) if t is not None]
    filled = list(times)
    count = 0
    for a, b in zip(known, known[1:]):
        span = dists[b] - dists[a]
        for i in range(a + 1, b):
            frac = 0.0 if span <= 0 else (dists[i] - dists[a]) / span
            filled[i] = int(round(times[a] + frac * (times[b] - times[a])))
            count += 1
    return filled, count


def _read_shapes(directory: Path, wanted: set) -> dict:
    """shape_id -> flat array('d') [seq, lat, lon, ...]; order-independent and compact."""
    shapes: dict[str, array] = {}
    for row in _rows(directory, "shapes", required=False) or ():
        sid = row.get("shape_id")
        if sid not in wanted:
            continue
        try:
            values = (float(row["shape_pt_sequence"]), float(row["shape_pt_lat"]), float(row["shape_pt_lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        shapes.setdefault(sid, array("d")).extend(values)
    return shapes


def _bulk(model, rows, factory, batch=BATCH):
    """bulk_create from compact tuples without materialising every model instance at once."""
    buffer = []
    for row in rows:
        buffer.append(factory(row))
        if len(buffer) >= batch:
            model.objects.bulk_create(buffer)
            buffer.clear()
    if buffer:
        model.objects.bulk_create(buffer)


def _shape_points(flat: array):
    triples = sorted(zip(flat[0::3], flat[1::3], flat[2::3]))
    return [(lat, lon) for _, lat, lon in triples]


GTFS_FILES = {
    "agency.txt", "calendar.txt", "calendar_dates.txt", "feed_info.txt", "routes.txt",
    "shapes.txt", "stop_times.txt", "stops.txt", "trips.txt",
}


def download_gtfs(url: str, destination: Path) -> Path:
    """
    Download a GTFS zip and extract only the known .txt files into ``destination``.
    Member names are reduced to their base name, so a crafted archive cannot write
    outside the folder.
    """
    import shutil
    import tempfile
    import zipfile

    from apps.buses.services.apsrtc_service import build_session

    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as tmp:
        with build_session().get(url, stream=True, timeout=(10, 120)) as response:
            if response.status_code != 200:
                raise GtfsImportError(f"{url} returned HTTP {response.status_code}")
            for chunk in response.iter_content(chunk_size=1 << 20):
                tmp.write(chunk)
        tmp.seek(0)
        try:
            archive = zipfile.ZipFile(tmp)
        except zipfile.BadZipFile as exc:
            raise GtfsImportError(f"{url} is not a zip file") from exc
        with archive:
            for member in archive.infolist():
                name = Path(member.filename).name
                if member.is_dir() or name not in GTFS_FILES:
                    continue
                with archive.open(member) as src, (destination / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    if not (destination / "stops.txt").exists():
        raise GtfsImportError(f"{url} does not contain stops.txt")
    return destination


def import_gtfs(directory, *, source_label: str = "") -> ImportStats:
    started = time.monotonic()
    directory = Path(directory)
    stats = ImportStats()

    agency = next(_rows(directory, "agency", required=False) or iter(()), {}) or {}
    feed_info = next(_rows(directory, "feed_info", required=False) or iter(()), {}) or {}
    calendar = list(_rows(directory, "calendar", required=False) or ())

    stops = {}
    for row in _rows(directory, "stops"):
        try:
            lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if row.get("location_type") not in (None, "", "0"):
            continue  # stations / entrances: buses stop at platforms (location_type 0)
        stops[row["stop_id"]] = (row.get("stop_name") or "Unnamed stop", lat, lon)

    routes = {row["route_id"]: row for row in _rows(directory, "routes")}
    trips = {row["trip_id"]: row for row in _rows(directory, "trips") if row.get("route_id") in routes}

    trip_stops = defaultdict(list)
    for row in _rows(directory, "stop_times"):
        tid = row.get("trip_id")
        if tid in trips and row.get("stop_id") in stops:
            try:
                seq = int(row["stop_sequence"])
            except (KeyError, TypeError, ValueError):
                continue
            trip_stops[tid].append((seq, row["stop_id"], parse_gtfs_time(row.get("arrival_time") or row.get("departure_time"))))

    # ---- patterns ------------------------------------------------------------------
    grouped: dict[tuple, list[str]] = defaultdict(list)
    for tid, rows in trip_stops.items():
        rows.sort()
        if len(rows) < 2:
            stats.skipped_trips += 1
            continue
        trip = trips[tid]
        key = (trip["route_id"], trip.get("shape_id") or "", tuple((s, sid) for s, sid, _ in rows))
        grouped[key].append(tid)
    # Sorted by route, busiest first: pattern ids are deterministic and a route's main pattern comes first.
    patterns = sorted(grouped.items(), key=lambda kv: (kv[0][0], -len(kv[1])))
    del grouped

    # ---- shapes: one at a time (decoded geometry is large; never hold all of it) ----
    by_shape = defaultdict(list)
    for pos, (key, _) in enumerate(patterns):
        if key[1]:
            by_shape[key[1]].append(pos)
    located_at = [None] * len(patterns)
    shape_rows = []
    raw_shapes = _read_shapes(directory, set(by_shape))
    for sid in sorted(raw_shapes):
        points = _shape_points(raw_shapes.pop(sid))
        if len(points) < 2:
            continue
        polyline = Polyline(points)
        fits = False
        for pos in by_shape[sid]:
            coords = [(stops[s][1], stops[s][2]) for _, s in patterns[pos][0][2]]
            located = locate_stops(polyline, coords, max_offset_m=STOP_SNAP_M)
            if statistics.median(off for _, off in located) <= MAX_SHAPE_FIT_M:
                located_at[pos] = located
                fits = True
        if fits:
            shape_rows.append((sid, encode_polyline(polyline.points), len(polyline.points), round(polyline.length_m, 1)))
    del raw_shapes, by_shape

    # Compact tuples; model instances are only created in small batches at write time.
    pattern_rows, pattern_stop_rows, trip_rows = [], [], []
    route_stops = defaultdict(set)
    route_trip_types = defaultdict(Counter)
    route_trip_counts = Counter()
    main_pattern_ends = {}

    for pid, (key, tids) in enumerate(patterns, start=1):
        route_id, shape_id, seq_stops = key
        located = located_at[pid - 1]
        approximate = located is None
        if approximate:
            stats.approximate_patterns += 1
            coords = [(stops[s][1], stops[s][2]) for _, s in seq_stops]
            dists, total = [0.0], 0.0
            for a, b in zip(coords, coords[1:]):
                total += haversine_m(a[0], a[1], b[0], b[1])
                dists.append(total)
            located = [(d, 0.0) for d in dists]
        main_pattern_ends.setdefault(route_id, (seq_stops[0][1], seq_stops[-1][1]))

        headsign = Counter(trips[t].get("trip_headsign") or "" for t in tids).most_common(1)[0][0]
        pattern_rows.append((
            pid, route_id, None if approximate else shape_id, headsign, len(seq_stops), len(tids),
            round(located[-1][0] - located[0][0], 1), approximate,
        ))
        dists = [d for d, _ in located]
        for index, ((seq, sid), (dist, offset)) in enumerate(zip(seq_stops, located)):
            pattern_stop_rows.append((pid, index, seq, sid, round(dist, 1), round(offset, 1)))
            route_stops[route_id].add(sid)

        for tid in tids:
            trip = trips[tid]
            times, filled = _interpolate([t for _, _, t in trip_stops.pop(tid)], dists)
            stats.interpolated_times += filled
            start = next((t for t in times if t is not None), None)
            offsets = [None if t is None or start is None else t - start for t in times]
            trip_rows.append((
                tid, route_id, pid, trip.get("trip_headsign") or "", trip.get("trip_short_name") or "",
                trip.get("trip_type") or "", start, offsets,
            ))
            route_trip_counts[route_id] += 1
            if trip.get("trip_type"):
                route_trip_types[route_id][trip["trip_type"]] += 1
    del patterns, located_at, trip_stops

    # ---- routes ------------------------------------------------------------------
    route_rows = []
    for rid, row in routes.items():
        if rid not in route_trip_counts:
            continue
        origin, destination = _split_long_name(row.get("route_long_name") or "")
        if not origin and rid in main_pattern_ends:
            first, last = main_pattern_ends[rid]
            origin, destination = stops[first][0], stops[last][0]
        lats = [stops[s][1] for s in route_stops[rid]]
        lons = [stops[s][2] for s in route_stops[rid]]
        route_rows.append(Route(
            route_id=rid,
            short_name=row.get("route_short_name") or "",
            long_name=row.get("route_long_name") or "",
            description=(row.get("route_desc") or "")[:255],
            service_type=_service_type(row.get("route_desc") or ""),
            bus_type=(route_trip_types[rid].most_common(1) or [("", 0)])[0][0],
            origin_name=origin, destination_name=destination,
            trip_count=route_trip_counts[rid],
            min_lat=min(lats), min_lon=min(lons), max_lat=max(lats), max_lon=max(lons),
        ))

    used_stop_ids = {row[3] for row in pattern_stop_rows}
    stop_rows = [Stop(stop_id=sid, name=v[0], latitude=v[1], longitude=v[2]) for sid, v in stops.items() if sid in used_stop_ids]

    starts = [_parse_date(c.get("start_date")) for c in calendar]
    ends = [_parse_date(c.get("end_date")) for c in calendar]

    with transaction.atomic():
        Trip.objects.all().delete()
        PatternStop.objects.all().delete()
        Pattern.objects.all().delete()
        Shape.objects.all().delete()
        Route.objects.all().delete()
        Stop.objects.all().delete()
        Stop.objects.bulk_create(stop_rows, batch_size=BATCH)
        Route.objects.bulk_create(route_rows, batch_size=BATCH)
        _bulk(Shape, shape_rows, lambda r: Shape(shape_id=r[0], encoded=r[1], point_count=r[2], length_m=r[3]), 200)
        _bulk(Pattern, pattern_rows, lambda r: Pattern(
            id=r[0], route_id=r[1], shape_id=r[2], headsign=r[3], stop_count=r[4], trip_count=r[5],
            length_m=r[6], path_is_approximate=r[7],
        ))
        _bulk(PatternStop, pattern_stop_rows, lambda r: PatternStop(
            pattern_id=r[0], index=r[1], stop_sequence=r[2], stop_id=r[3], dist_m=r[4], snap_offset_m=r[5],
        ))
        _bulk(Trip, trip_rows, lambda r: Trip(
            trip_id=r[0], route_id=r[1], pattern_id=r[2], headsign=r[3], short_name=r[4], bus_type=r[5],
            start_time_s=r[6], arrival_offsets=r[7],
        ))
        GtfsFeed.objects.filter(is_active=True).update(is_active=False)
        GtfsFeed.objects.create(
            version=feed_info.get("feed_version", "")[:64],
            publisher=(feed_info.get("feed_publisher_name") or agency.get("agency_name") or "")[:255],
            timezone=agency.get("agency_timezone") or "Asia/Kolkata",
            source=(source_label or str(directory))[:500],
            service_start=min((d for d in starts if d), default=None),
            service_end=max((d for d in ends if d), default=None),
            route_count=len(route_rows), stop_count=len(stop_rows), trip_count=len(trip_rows),
            pattern_count=len(pattern_rows), shape_count=len(shape_rows),
            approximate_pattern_count=stats.approximate_patterns,
        )

    stats.stops, stats.routes, stats.trips = len(stop_rows), len(route_rows), len(trip_rows)
    stats.patterns, stats.shapes = len(pattern_rows), len(shape_rows)
    stats.seconds = round(time.monotonic() - started, 1)
    logger.info("GTFS import finished: %s", stats)
    return stats
