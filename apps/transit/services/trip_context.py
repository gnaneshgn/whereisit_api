"""
Cached, read-only view of one trip: its pattern stops, path and timings.

Shapes and pattern stop lists are immutable for a given GTFS import, so they
are memoised per process keyed by the active feed id. A re-import changes the
feed id, which naturally invalidates the memo.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from apps.transit.models import GtfsFeed, PatternStop, Shape, Trip
from apps.transit.services.geometry import Polyline, decode_polyline

_FEED_TTL_SECONDS = 30
_feed_memo = {"at": 0.0, "feed": None}


def active_feed() -> GtfsFeed | None:
    now = time.monotonic()
    if now - _feed_memo["at"] > _FEED_TTL_SECONDS:
        _feed_memo["feed"] = GtfsFeed.objects.filter(is_active=True).order_by("-imported_at").first()
        _feed_memo["at"] = now
    return _feed_memo["feed"]


def reset_feed_memo():
    _feed_memo["at"] = 0.0
    _feed_memo["feed"] = None
    _pattern_stops.cache_clear()
    _polyline.cache_clear()


@dataclass(frozen=True)
class StopInfo:
    index: int
    stop_sequence: int
    stop_id: str
    name: str
    latitude: float
    longitude: float
    dist_m: float

    def ref(self) -> dict:
        return {"stop_id": self.stop_id, "stop_name": self.name, "stop_sequence": self.stop_sequence}


@lru_cache(maxsize=4096)
def _pattern_stops(feed_id: int, pattern_id: int) -> tuple[StopInfo, ...]:
    rows = (
        PatternStop.objects.filter(pattern_id=pattern_id)
        .order_by("index")
        .values_list("index", "stop_sequence", "stop_id", "stop__name", "stop__latitude", "stop__longitude", "dist_m")
    )
    return tuple(StopInfo(*row) for row in rows)


@lru_cache(maxsize=2048)
def _polyline(feed_id: int, shape_id: str) -> Polyline | None:
    encoded = Shape.objects.filter(shape_id=shape_id).values_list("encoded", flat=True).first()
    return Polyline(decode_polyline(encoded)) if encoded else None


def pattern_stops(pattern_id: int) -> tuple[StopInfo, ...]:
    feed = active_feed()
    return _pattern_stops(feed.id if feed else 0, pattern_id)


def pattern_polyline(pattern) -> tuple[Polyline, bool]:
    """(polyline, is_approximate). Falls back to the line joining the stops."""
    feed = active_feed()
    if pattern.shape_id and not pattern.path_is_approximate:
        poly = _polyline(feed.id if feed else 0, pattern.shape_id)
        if poly is not None:
            return poly, False
    stops = pattern_stops(pattern.id)
    return Polyline([(s.latitude, s.longitude) for s in stops]), True


@dataclass
class TripContext:
    trip: Trip
    stops: tuple[StopInfo, ...]
    polyline: Polyline
    approximate: bool

    @property
    def route(self):
        return self.trip.route

    @property
    def pattern(self):
        return self.trip.pattern

    def offset(self, index: int) -> int | None:
        offsets = self.trip.arrival_offsets or []
        return offsets[index] if 0 <= index < len(offsets) else None

    def offset_at(self, along_m: float) -> float | None:
        """Scheduled seconds-from-start at an along-distance (linear between timed stops)."""
        stops = self.stops
        if along_m <= stops[0].dist_m:
            return self.offset(0)
        for prev, nxt in zip(stops, stops[1:]):
            if along_m <= nxt.dist_m:
                a, b = self.offset(prev.index), self.offset(nxt.index)
                if a is None or b is None:
                    return None
                span = nxt.dist_m - prev.dist_m
                frac = 0.0 if span <= 0 else (along_m - prev.dist_m) / span
                return a + frac * (b - a)
        return self.offset(stops[-1].index)

    def scheduled_at(self, index: int, service_date: date | None, tz: ZoneInfo) -> datetime | None:
        """Absolute scheduled arrival at a stop for the trip running on ``service_date``."""
        offset = self.offset(index)
        if service_date is None or offset is None or self.trip.start_time_s is None:
            return None
        midnight = datetime.combine(service_date, dtime(0, 0), tzinfo=tz)
        return midnight + timedelta(seconds=self.trip.start_time_s + offset)

    def find_index(self, stop_id: str | None = None, stop_sequence: int | None = None) -> int | None:
        for s in self.stops:
            if (stop_id is not None and s.stop_id == stop_id) or (
                stop_id is None and stop_sequence is not None and s.stop_sequence == stop_sequence
            ):
                return s.index
        return None


def feed_timezone() -> ZoneInfo:
    feed = active_feed()
    try:
        return ZoneInfo(feed.timezone if feed else "Asia/Kolkata")
    except Exception:  # unknown tz name in the feed
        return ZoneInfo("Asia/Kolkata")


def build_context(trip: Trip) -> TripContext | None:
    stops = pattern_stops(trip.pattern_id)
    if len(stops) < 2:
        return None
    polyline, approximate = pattern_polyline(trip.pattern)
    return TripContext(trip=trip, stops=stops, polyline=polyline, approximate=approximate)


def load_contexts(trip_ids) -> dict[str, TripContext]:
    """Contexts for many trips (used by the live sync). Missing trips are simply absent."""
    if active_feed() is None:
        return {}
    ids = [t for t in set(trip_ids) if t]
    contexts = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for trip in Trip.objects.filter(trip_id__in=chunk).select_related("route", "pattern"):
            ctx = build_context(trip)
            if ctx is not None:
                contexts[trip.trip_id] = ctx
    return contexts


def load_context(trip_id: str | None) -> TripContext | None:
    if not trip_id or active_feed() is None:
        return None
    trip = Trip.objects.filter(trip_id=trip_id).select_related("route", "pattern").first()
    return build_context(trip) if trip else None
