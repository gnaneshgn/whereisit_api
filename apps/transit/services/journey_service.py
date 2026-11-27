"""
Source -> destination planning on direct buses, with live arrivals.

Algorithm
---------
1. Resolve each end to candidate stops: an explicit stop plus stops within
   ``SAME_STOP_RADIUS_M`` (the platform across the road), or every stop within
   ``JOURNEY_WALK_RADIUS_M`` of a coordinate. Each candidate carries its walk.
2. Index lookup: PatternStop rows for origin candidates, then for destination
   candidates on the same patterns. A pattern serves the journey when an origin
   stop comes *before* a destination stop (index_o < index_d). Direction is
   therefore correct by construction.
3. Per pattern keep the pair with the least walking, then the shortest ride.
4. Live buses: primary LiveBus rows on those patterns whose stored progress is
   still before the boarding stop. ETA at boarding / alighting uses the same
   estimator as the bus timeline (realtime trip update, else schedule + delay).
5. Scheduled departures (timetable, clearly labelled) for trips with no live bus.
6. Options sorted by the earliest live arrival, then the earliest scheduled one.

Transfers are not planned yet: no direct bus -> empty ``options``.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, time as dtime

from django.conf import settings

from apps.buses.models import LiveBus
from apps.buses.services import bus_matching_service, feed_status_service, location_service
from apps.transit.models import Pattern, PatternStop, Stop, Trip
from apps.transit.services import trip_context
from apps.transit.services.progress_service import estimate_arrivals
from common.utilities import iso_utc, utc_now

LIVE_FIELDS = (
    "vehicle_id", "registration_number", "trip_id", "route_id", "latitude", "longitude",
    "gps_timestamp", "progress", "is_superseded",
)


def _cfg(key):
    return settings.TRANSIT[key]


def stops_near(latitude: float, longitude: float, radius_m: float, limit: int = 50):
    """[(Stop, distance_m)] nearest first."""
    bbox = location_service.bbox_around(latitude, longitude, radius_m / 1000.0)
    qs = location_service.filter_bbox(Stop.objects.all(), bbox)
    pairs = location_service.within_radius(qs, latitude, longitude, radius_m / 1000.0, limit)
    return [(stop, km * 1000.0) for stop, km in pairs]


def resolve_endpoint(stop_id=None, latitude=None, longitude=None):
    """Candidate stops for one end of the journey: [(Stop, walk_m)]."""
    if stop_id:
        stop = Stop.objects.filter(stop_id=stop_id).first()
        if stop is None:
            return None
        nearby = stops_near(stop.latitude, stop.longitude, _cfg("SAME_STOP_RADIUS_M"), limit=6)
        return [(stop, 0.0)] + [(s, d) for s, d in nearby if s.stop_id != stop.stop_id]
    return stops_near(latitude, longitude, _cfg("JOURNEY_WALK_RADIUS_M"), limit=12)


def _stop_ref(info, walk_m=None):
    ref = {**info.ref(), "latitude": info.latitude, "longitude": info.longitude}
    if walk_m is not None:
        ref["walk_m"] = round(walk_m)
    return ref


def _route_ref(route):
    return {
        "route_id": route.route_id, "route_number": route.short_name or None, "route_name": route.long_name or None,
        "service_type": route.service_type, "bus_type": route.bus_type or None,
    }


def _matching_pairs(origins, destinations):
    """{pattern_id: (o_index, d_index, walk_o, walk_d)} for patterns serving o -> d."""
    walk_o = {s.stop_id: w for s, w in origins}
    walk_d = {s.stop_id: w for s, w in destinations}
    o_rows = defaultdict(list)
    for pid, index, sid in PatternStop.objects.filter(stop_id__in=list(walk_o)).values_list("pattern_id", "index", "stop_id"):
        o_rows[pid].append((index, sid))
    if not o_rows:
        return {}
    best = {}
    d_qs = PatternStop.objects.filter(stop_id__in=list(walk_d), pattern_id__in=list(o_rows)).values_list(
        "pattern_id", "index", "stop_id"
    )
    for pid, d_index, d_sid in d_qs:
        for o_index, o_sid in o_rows[pid]:
            if o_index >= d_index:
                continue
            cand = (walk_o[o_sid] + walk_d[d_sid], d_index - o_index, o_index, d_index, walk_o[o_sid], walk_d[d_sid])
            if pid not in best or cand < best[pid]:
                best[pid] = cand
    return {pid: (c[2], c[3], c[4], c[5]) for pid, c in best.items()}


def _service_dates(now_local):
    """Today and yesterday (GTFS times past 24:00 belong to the previous service day)."""
    today = now_local.date()
    return [today, today - timedelta(days=1)]


def _scheduled_departures(pattern_ids, board_index, now, tz, exclude_trip_ids, per_pattern=3):
    """{pattern_id: [{trip_id, departs_at, arrives_at_index}]} within the schedule window."""
    window = timedelta(minutes=_cfg("SCHEDULE_WINDOW_MIN"))
    now_local = now.astimezone(tz)
    out = defaultdict(list)
    trips = Trip.objects.filter(pattern_id__in=pattern_ids).exclude(start_time_s=None).values_list(
        "trip_id", "pattern_id", "start_time_s", "arrival_offsets"
    )
    for trip_id, pid, start, offsets in trips:
        if trip_id in exclude_trip_ids:
            continue
        o_index = board_index[pid]
        if o_index >= len(offsets) or offsets[o_index] is None:
            continue
        for service_date in _service_dates(now_local):
            midnight = datetime.combine(service_date, dtime(0, 0), tzinfo=tz)
            departs = midnight + timedelta(seconds=start + offsets[o_index])
            if now <= departs <= now + window:
                out[pid].append((departs, trip_id, start, offsets, midnight))
    result = {}
    for pid, items in out.items():
        items.sort()
        result[pid] = items[:per_pattern]
    return result


def _live_on_patterns(patterns: dict):
    """{pattern_id: [LiveBus]} for primary buses with progress on these patterns."""
    by_pattern = defaultdict(list)
    route_ids = list({p.route_id for p in patterns.values()})
    qs = LiveBus.objects.filter(is_superseded=False, route_id__in=route_ids, progress__isnull=False).only(*LIVE_FIELDS)
    for bus in qs:
        pid = (bus.progress or {}).get("pattern_id")
        if pid in patterns:
            by_pattern[pid].append(bus)
    return by_pattern


def _trip_update_for(updates: dict, bus):
    update = updates.get(bus.vehicle_id)
    if update and update.get("trip_id") and bus.trip_id and update["trip_id"] != bus.trip_id:
        return None
    return update


def _approaching(bus, ctx, o_index, d_index, now, updates):
    """Live entry for a bus that has not yet passed the boarding stop, else None."""
    progress = bus.progress or {}
    along = progress.get("distance_along_m")
    if along is None or progress.get("status") == "off_route":
        return None
    board, alight = ctx.stops[o_index], ctx.stops[d_index]
    if along > board.dist_m + _cfg("AT_STOP_M"):
        return None  # already past the boarding stop
    next_seq = (progress.get("next_stop") or {}).get("stop_sequence")
    next_index = ctx.find_index(stop_sequence=next_seq) if next_seq is not None else o_index
    etas = estimate_arrivals(ctx, along, bus.gps_timestamp, _trip_update_for(updates, bus), [board, alight])
    return {
        "vehicle_id": bus.vehicle_id,
        "registration_number": bus.registration_number,
        "trip_id": bus.trip_id,
        "latitude": bus.latitude,
        "longitude": bus.longitude,
        "gps_status": feed_status_service.classify_gps_status(bus.gps_timestamp, now),
        "gps_timestamp": iso_utc(bus.gps_timestamp),
        "stops_away": max(0, o_index - (next_index if next_index is not None else o_index)),
        "distance_to_board_km": round(max(0.0, board.dist_m - along) / 1000.0, 2),
        "eta_at_board": etas[board.index]["expected_arrival"],
        "eta_at_alight": etas[alight.index]["expected_arrival"],
        "eta_source": etas[board.index]["eta_source"],
    }


def plan(origins, destinations, *, include_path=False, limit=10):
    now = utc_now()
    tz = trip_context.feed_timezone()
    pairs = _matching_pairs(origins, destinations)
    if not pairs:
        return []
    patterns = {p.id: p for p in Pattern.objects.filter(id__in=list(pairs)).select_related("route")}
    live = _live_on_patterns(patterns)
    updates = bus_matching_service.get_trip_updates_map()

    # One representative trip per pattern for timings (the scheduled ride time).
    rep_trip = {}
    for trip in Trip.objects.filter(pattern_id__in=list(pairs)).select_related("route", "pattern").order_by("trip_id"):
        rep_trip.setdefault(trip.pattern_id, trip)
    live_trip_ids = {b.trip_id for buses in live.values() for b in buses}
    scheduled = _scheduled_departures(list(pairs), {pid: v[0] for pid, v in pairs.items()}, now, tz, live_trip_ids)

    options = []
    for pid, (o_index, d_index, walk_o, walk_d) in pairs.items():
        pattern = patterns.get(pid)
        trip = rep_trip.get(pid)
        if pattern is None or trip is None:
            continue
        ctx = trip_context.build_context(trip)
        if ctx is None:
            continue
        board, alight = ctx.stops[o_index], ctx.stops[d_index]
        ride_offsets = (ctx.offset(o_index), ctx.offset(d_index))

        live_entries = []
        trip_ctx_cache = {}
        for bus in live.get(pid, []):
            bctx = trip_ctx_cache.get(bus.trip_id)
            if bctx is None:
                bctx = trip_ctx_cache[bus.trip_id] = trip_context.load_context(bus.trip_id) or ctx
            entry = _approaching(bus, bctx, o_index, d_index, now, updates)
            if entry:
                live_entries.append(entry)
        live_entries.sort(key=lambda e: (e["eta_at_board"] or "9999", e["distance_to_board_km"]))

        upcoming = []
        for departs, trip_id, start, offsets, midnight in scheduled.get(pid, []):
            arrive = offsets[d_index] if d_index < len(offsets) else None
            upcoming.append({
                "trip_id": trip_id,
                "departs_at": iso_utc(departs),
                "arrives_at": iso_utc(midnight + timedelta(seconds=start + arrive)) if arrive is not None else None,
                "source": "schedule",
            })

        option = {
            "pattern_id": pid,
            "route": _route_ref(pattern.route),
            "towards": ctx.stops[-1].name,      # last stop of this bus: what to look for
            "headsign": pattern.headsign or None,  # raw GTFS text, may not match "towards"
            "board": _stop_ref(board, walk_o),
            "alight": _stop_ref(alight, walk_d),
            "stop_count": d_index - o_index,
            "stops": [
                {**_stop_ref(s), "distance_from_board_km": round((s.dist_m - board.dist_m) / 1000.0, 2)}
                for s in ctx.stops[o_index:d_index + 1]
            ],
            "ride_distance_km": round((alight.dist_m - board.dist_m) / 1000.0, 2),
            "scheduled_ride_minutes": (
                round((ride_offsets[1] - ride_offsets[0]) / 60.0)
                if None not in ride_offsets else None
            ),
            "live_buses": live_entries[:5],
            "next_scheduled": upcoming,
            "path_approximate": ctx.approximate,
        }
        if include_path:
            option["path"] = [[round(a, 6), round(b, 6)] for a, b in ctx.polyline.slice(board.dist_m, alight.dist_m)]
        options.append(option)

    def sort_key(o):
        live_eta = o["live_buses"][0]["eta_at_board"] if o["live_buses"] and o["live_buses"][0]["eta_at_board"] else None
        sched = o["next_scheduled"][0]["departs_at"] if o["next_scheduled"] else None
        return (live_eta is None, live_eta or "", sched is None, sched or "", o["board"]["walk_m"] + o["alight"]["walk_m"], o["ride_distance_km"])

    options.sort(key=sort_key)
    return options[:limit]


def arrivals(stop: Stop, *, limit=20):
    """Live buses approaching ``stop`` (any pattern that serves it before its last stop)."""
    now = utc_now()
    rows = list(PatternStop.objects.filter(stop=stop).values_list("pattern_id", "index"))
    board_index = {pid: idx for pid, idx in rows}
    patterns = {p.id: p for p in Pattern.objects.filter(id__in=list(board_index)).select_related("route")}
    board_index = {pid: idx for pid, idx in board_index.items() if pid in patterns and idx < patterns[pid].stop_count - 1}
    patterns = {pid: p for pid, p in patterns.items() if pid in board_index}
    live = _live_on_patterns(patterns)
    updates = bus_matching_service.get_trip_updates_map()
    entries = []
    for pid, buses in live.items():
        for bus in buses:
            ctx = trip_context.load_context(bus.trip_id)
            if ctx is None:
                continue
            o_index = board_index[pid]
            entry = _approaching(bus, ctx, o_index, len(ctx.stops) - 1, now, updates)
            if entry:
                entry.pop("eta_at_alight", None)
                entry["route"] = _route_ref(patterns[pid].route)
                entry["towards"] = ctx.stops[-1].name
                entry["headsign"] = patterns[pid].headsign or None
                entries.append(entry)
    entries.sort(key=lambda e: (e["eta_at_board"] is None, e["eta_at_board"] or "", e["distance_to_board_km"]))
    return entries[:limit]
