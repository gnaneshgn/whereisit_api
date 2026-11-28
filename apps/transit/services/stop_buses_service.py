"""
Live buses that serve a stop (or every stop matching a name, e.g. "Simhachalam").

A bus serves the stop when its *trip* stops there, found from the trip's ordered
stop list (stop_times), not from route names. For each bus:

  relation  starts | ends | passes     where the stop sits in the trip
  status    approaching | at_stop | passed | unknown
            from the bus's stored progress (fresh GPS only); unknown when the GPS
            is stale or the bus is off route, so it is never guessed
  category  metro | local | other      from the bus's own trip_type

If a trip serves several matching stops (Simhachalam and Simhachalam Hill), the
next one ahead of the bus is reported.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from django.conf import settings

from apps.buses import selectors as bus_selectors
from apps.buses.serializers import LiveBusSerializer, usable_progress
from apps.buses.services import bus_matching_service
from apps.transit.models import PatternStop, Trip
from apps.transit.services import trip_context
from apps.transit.services.progress_service import estimate_arrivals

STATUS_ORDER = {"at_stop": 0, "approaching": 1, "unknown": 2, "passed": 3}


def _bus_patterns(buses, pattern_ids) -> dict:
    """{trip_id: pattern_id} for live buses whose trip runs one of ``pattern_ids``."""
    trip_ids = list({b.trip_id for b in buses if b.trip_id})
    out = {}
    for start in range(0, len(trip_ids), 500):
        rows = Trip.objects.filter(trip_id__in=trip_ids[start:start + 500], pattern_id__in=pattern_ids)
        out.update(rows.values_list("trip_id", "pattern_id"))
    return out


def buses_via_stops(stops, *, now, category=None) -> dict:
    """{"counts": {...}, "results": [bus + via]} for live buses serving any of ``stops``."""
    names = {s.stop_id: s.name for s in stops}
    by_pattern = defaultdict(list)
    rows = PatternStop.objects.filter(stop_id__in=list(names)).values_list(
        "pattern_id", "index", "stop_id", "dist_m", "pattern__stop_count"
    )
    for pid, index, sid, dist, count in rows:
        by_pattern[pid].append((index, sid, dist, count))
    if not by_pattern:
        return {"counts": {"all": 0, "metro": 0, "local": 0, "other": 0}, "results": []}

    buses = list(bus_selectors.live_buses(now=now).exclude(trip_id=None))
    trip_pattern = _bus_patterns(buses, list(by_pattern))
    updates = bus_matching_service.get_trip_updates_map()
    at_stop_m = settings.TRANSIT["AT_STOP_M"]
    serializer_ctx = {"now": now}

    results = []
    for bus in buses:
        pid = trip_pattern.get(bus.trip_id)
        if pid is None:
            continue
        matches = sorted(by_pattern[pid])
        progress = usable_progress(bus, now)
        along = (progress or {}).get("distance_along_m")
        if progress and progress.get("pattern_id") != pid:
            along = None

        if along is None:
            index, sid, dist, count = matches[0]
            status = "unknown"
        else:
            ahead = [m for m in matches if m[2] >= along - at_stop_m]
            index, sid, dist, count = ahead[0] if ahead else matches[-1]
            status = "passed" if not ahead else ("at_stop" if abs(dist - along) <= at_stop_m else "approaching")

        via = {
            "stop_id": sid,
            "stop_name": names[sid],
            "relation": "starts" if index == 0 else "ends" if index == count - 1 else "passes",
            "status": status,
            "stops_away": None,
            "distance_km": None,
            "eta": None,
            "eta_source": None,
        }
        if status in ("approaching", "at_stop"):
            ctx = trip_context.load_context(bus.trip_id)
            if ctx is not None:
                next_seq = (progress.get("next_stop") or {}).get("stop_sequence")
                next_index = ctx.find_index(stop_sequence=next_seq) if next_seq is not None else None
                via["stops_away"] = 0 if status == "at_stop" else max(0, index - next_index) if next_index is not None else None
                via["distance_km"] = round(max(0.0, dist - along) / 1000.0, 2)
                update = updates.get(bus.vehicle_id)
                if update and update.get("trip_id") and update["trip_id"] != bus.trip_id:
                    update = None
                eta = estimate_arrivals(ctx, along, bus.gps_timestamp, update, [ctx.stops[index]])[index]
                via["eta"], via["eta_source"] = eta["expected_arrival"], eta["eta_source"]

        data = LiveBusSerializer(bus, context=serializer_ctx).data
        data["via"] = via
        results.append(data)

    counts = Counter(r["bus_category"] for r in results)
    summary = {"all": len(results), "metro": counts["metro"], "local": counts["local"], "other": counts["other"]}
    if category:
        results = [r for r in results if r["bus_category"] == category]
    results.sort(key=lambda r: (
        STATUS_ORDER[r["via"]["status"]],
        r["via"]["eta"] or "9999",
        r["via"]["distance_km"] if r["via"]["distance_km"] is not None else 1e9,
        r["registration_number"] or "",
    ))
    return {"counts": summary, "results": results}
