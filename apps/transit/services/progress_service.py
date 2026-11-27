"""
Where is a bus on its trip, and when will it reach each stop?

Algorithm (per GPS fix)
-----------------------
1. The live feed gives the bus's ``trip_id``; the trip gives its pattern: the
   exact ordered stops and the road shape. Direction is therefore known and
   never guessed.
2. Project the GPS point onto the shape. Every place the shape passes within
   ``OFF_ROUTE_M`` is a candidate (loops / out-and-back roads give several).
3. Score candidates and keep the lowest:
       offset (m from the road)
     + bearing penalty    bus heading vs road direction (only when moving)
     + continuity penalty going backwards from the previous fix is implausible
     + hint penalty       outside the window implied by the realtime trip update
4. Along-distance vs the stops' stored distances gives last-passed / current /
   next stop. Within ``AT_STOP_M`` of a stop counts as being at it.
5. No candidate within ``OFF_ROUTE_M`` -> ``off_route``; no stop is reported.

ETAs (per upcoming stop j), in order of preference
  * realtime: the trip update's prediction for stop k, then
    eta_j = eta_k + (scheduled_j - scheduled_k)           (delay propagation)
  * position: eta_j = gps_time + (scheduled_j - scheduled_at_bus_position)
    i.e. the bus keeps its current delay; works without a service date.
  * none: no timings in the source -> null. Nothing is invented.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings

from apps.transit.services.geometry import bearing_difference
from apps.transit.services.trip_context import TripContext
from common.utilities import iso_utc, utc_now

MOVING_SPEED_MS = 2.0          # below this the GPS bearing is unreliable
BEARING_OK_DEG = 60.0
BEARING_BAD_DEG = 120.0
BACKWARD_TOLERANCE_M = 200.0


class ProgressStatus:
    AT_STOP = "at_stop"
    IN_TRANSIT = "in_transit"
    NOT_STARTED = "not_started"   # at/before the first stop
    COMPLETED = "completed"       # at/after the last stop
    OFF_ROUTE = "off_route"


def _cfg(key):
    return settings.TRANSIT[key]


def _hint_window(ctx: TripContext, trip_update: dict | None):
    """Along-distance range the realtime feed implies (between previous and next stop)."""
    nxt = (trip_update or {}).get("next_stop") or {}
    if not nxt:
        return None
    index = ctx.find_index(stop_id=nxt.get("stop_id")) if nxt.get("stop_id") else None
    if index is None and nxt.get("stop_sequence") is not None:
        index = ctx.find_index(stop_sequence=nxt.get("stop_sequence"))
    if index is None:
        return None
    lo = ctx.stops[index - 1].dist_m if index > 0 else ctx.stops[0].dist_m
    return lo - 150.0, ctx.stops[index].dist_m + 150.0


def locate_bus(ctx: TripContext, latitude, longitude, *, bearing=None, speed=None, prev_along=None, trip_update=None):
    """Best projection of the bus onto its trip path, or None when off route."""
    candidates = ctx.polyline.candidates(latitude, longitude, _cfg("OFF_ROUTE_M"))
    if not candidates:
        return None
    window = _hint_window(ctx, trip_update)
    moving = bearing is not None and speed is not None and speed >= MOVING_SPEED_MS

    def score(c):
        s = c.offset_m
        if moving and not ctx.approximate:
            diff = bearing_difference(bearing, c.bearing)
            s += 0.0 if diff <= BEARING_OK_DEG else (150.0 if diff <= BEARING_BAD_DEG else 300.0)
        if prev_along is not None and prev_along - c.along_m > BACKWARD_TOLERANCE_M:
            s += 400.0
        if window and not window[0] <= c.along_m <= window[1]:
            s += 250.0
        return s

    return min(candidates, key=score)


def compute_progress(ctx: TripContext, latitude, longitude, *, bearing=None, speed=None, gps_timestamp=None,
                     prev_along=None, trip_update=None) -> dict:
    """
    Compact progress record (stored on LiveBus at every sync):

        {"status", "distance_along_m", "route_length_m", "progress_pct", "offset_m",
         "last_stop", "current_stop", "next_stop", "stops_remaining", "path_approximate", "as_of"}
    """
    stops = ctx.stops
    base = {"path_approximate": ctx.approximate, "as_of": iso_utc(gps_timestamp), "pattern_id": ctx.trip.pattern_id}
    proj = locate_bus(ctx, latitude, longitude, bearing=bearing, speed=speed, prev_along=prev_along, trip_update=trip_update)
    if proj is None:
        return {**base, "status": ProgressStatus.OFF_ROUTE, "distance_along_m": None, "last_stop": None,
                "current_stop": None, "next_stop": None, "stops_remaining": None, "progress_pct": None,
                "route_length_m": round(stops[-1].dist_m - stops[0].dist_m)}

    along = proj.along_m
    at_stop_m = _cfg("AT_STOP_M")
    # Stops already reached: dist <= along (+ the at-stop radius for the one we're standing at)
    current = next((s for s in stops if abs(s.dist_m - along) <= at_stop_m), None)
    passed = [s for s in stops if s.dist_m <= along + (at_stop_m if current is not None else 0.0)]
    last = passed[-1] if passed else None
    upcoming = [s for s in stops if s.dist_m > along + (at_stop_m if current is not None else 0.0)]
    nxt = upcoming[0] if upcoming else None

    if nxt is None:
        status = ProgressStatus.COMPLETED
    elif last is None or (current is not None and current.index == 0):
        status = ProgressStatus.NOT_STARTED
    elif current is not None:
        status = ProgressStatus.AT_STOP
    else:
        status = ProgressStatus.IN_TRANSIT

    start, end = stops[0].dist_m, stops[-1].dist_m
    length = max(1.0, end - start)
    next_stop = None
    if nxt is not None:
        next_stop = {**nxt.ref(), "distance_m": round(max(0.0, nxt.dist_m - along))}
        eta = estimate_arrivals(ctx, along, gps_timestamp, trip_update, [nxt])
        next_stop.update(eta.get(nxt.index, {"expected_arrival": None, "eta_source": None}))

    return {
        **base,
        "status": status,
        "distance_along_m": round(along, 1),
        "route_length_m": round(length),
        "progress_pct": round(100.0 * min(1.0, max(0.0, (along - start) / length)), 1),
        "offset_m": round(proj.offset_m, 1),
        "last_stop": last.ref() if last else None,
        "current_stop": current.ref() if current else None,
        "next_stop": next_stop,
        "stops_remaining": len(upcoming),
    }


def parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _realtime_anchor(ctx: TripContext, trip_update: dict | None, now: datetime):
    """(index, eta datetime) from the trip update, if it is for this trip and recent."""
    if not trip_update:
        return None
    if trip_update.get("trip_id") and trip_update["trip_id"] != ctx.trip.trip_id:
        return None
    nxt = trip_update.get("next_stop") or {}
    expected = parse_iso(nxt.get("expected_arrival"))
    if expected is None:
        return None
    updated = parse_iso(trip_update.get("updated_at"))
    if updated is not None and (now - updated).total_seconds() > _cfg("TRIP_UPDATE_MAX_AGE"):
        return None
    index = ctx.find_index(stop_id=nxt.get("stop_id")) if nxt.get("stop_id") else None
    if index is None and nxt.get("stop_sequence") is not None:
        index = ctx.find_index(stop_sequence=nxt.get("stop_sequence"))
    if index is None:
        return None
    return index, expected


def estimate_arrivals(ctx: TripContext, along_m: float, gps_timestamp, trip_update, targets) -> dict:
    """{stop_index: {"expected_arrival": iso|None, "eta_source": "realtime"|"schedule"|None}}"""
    result = {}
    now = utc_now()
    anchor = _realtime_anchor(ctx, trip_update, now)
    offset_here = ctx.offset_at(along_m) if along_m is not None else None
    for stop in targets:
        eta, source = None, None
        off_j = ctx.offset(stop.index)
        if anchor and stop.index >= anchor[0]:
            off_k = ctx.offset(anchor[0])
            if off_j is not None and off_k is not None:
                eta, source = anchor[1] + timedelta(seconds=off_j - off_k), "realtime"
        if eta is None and gps_timestamp is not None and off_j is not None and offset_here is not None:
            eta, source = gps_timestamp + timedelta(seconds=max(0.0, off_j - offset_here)), "schedule"
        if eta is not None and eta < now:
            eta = now  # due now; never show an arrival in the past for a stop still ahead
        result[stop.index] = {"expected_arrival": iso_utc(eta), "eta_source": source}
    return result


def stop_timeline(ctx: TripContext, progress: dict | None, *, gps_timestamp=None, trip_update=None,
                  service_date=None, tz=None, from_index: int | None = None, to_index: int | None = None) -> list[dict]:
    """
    Every stop of the trip (optionally only ``from_index..to_index``) with
    status passed / current / next / upcoming, distance from the bus and ETA.
    Without usable progress, stops carry only scheduled times (status null).
    """
    stops = ctx.stops
    lo = 0 if from_index is None else max(0, from_index)
    hi = len(stops) - 1 if to_index is None else min(len(stops) - 1, to_index)
    along = progress.get("distance_along_m") if progress else None
    next_seq = ((progress or {}).get("next_stop") or {}).get("stop_sequence")
    current_seq = ((progress or {}).get("current_stop") or {}).get("stop_sequence")

    upcoming = [s for s in stops[lo:hi + 1] if along is not None and (next_seq is not None and s.stop_sequence >= next_seq)]
    etas = estimate_arrivals(ctx, along, gps_timestamp, trip_update, upcoming) if upcoming else {}

    timeline = []
    for s in stops[lo:hi + 1]:
        if along is None:
            status = None
        elif current_seq is not None and s.stop_sequence == current_seq:
            status = "current"
        elif next_seq is None or s.stop_sequence < next_seq:
            status = "passed"
        elif s.stop_sequence == next_seq:
            status = "next"
        else:
            status = "upcoming"
        scheduled = ctx.scheduled_at(s.index, service_date, tz) if tz else None
        entry = {
            **s.ref(),
            "latitude": s.latitude,
            "longitude": s.longitude,
            "status": status,
            "distance_along_km": round((s.dist_m - stops[0].dist_m) / 1000.0, 2),
            "distance_km": round(max(0.0, s.dist_m - along) / 1000.0, 2) if status in ("next", "upcoming") else None,
            "scheduled_arrival": iso_utc(scheduled),
            "eta": None,
            "eta_source": None,
            "delay_seconds": None,
        }
        if s.index in etas:
            entry["eta"] = etas[s.index]["expected_arrival"]
            entry["eta_source"] = etas[s.index]["eta_source"]
            if entry["eta"] and scheduled is not None:
                entry["delay_seconds"] = int((parse_iso(entry["eta"]) - scheduled).total_seconds())
        timeline.append(entry)
    return timeline
