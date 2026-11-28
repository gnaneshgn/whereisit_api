"""Stop timeline for one live bus (bus detail and /buses/{id}/trip/)."""
from __future__ import annotations

from apps.buses.services import bus_matching_service
from apps.transit.route_numbers import display_for
from apps.transit.services import trip_context
from apps.transit.services.progress_service import stop_timeline


def path_points(polyline, start_m=None, end_m=None):
    points = polyline.points if start_m is None else polyline.slice(start_m, end_m)
    return [[round(lat, 6), round(lon, 6)] for lat, lon in points]


def route_block(ctx) -> dict:
    route = ctx.route
    return {
        "route_id": route.route_id,
        "route_number": display_for(route),
        "route_name": route.long_name or None,
        "service_type": route.service_type,
        "bus_type": ctx.trip.bus_type or route.bus_type or None,
        "start_stop_name": ctx.stops[0].name,
        "end_stop_name": ctx.stops[-1].name,
        # Raw GTFS headsign, informational only: it often names neither end of the trip.
        "headsign": ctx.trip.headsign or None,
        "pattern_id": ctx.trip.pattern_id,
        "path_approximate": ctx.approximate,
    }


def bus_timeline(bus, progress, *, from_stop_id=None, to_stop_id=None, include_path=True):
    """
    None when the bus has no GTFS trip. Otherwise:
      {"route": {..., "stops": [...], "path": [...]}, "stops": [...], "journey": {...} | None}
    ``journey`` (when ``to_stop_id`` is on this trip and still ahead) is what a rider
    needs: stops left until their stop, distance, and ETA there.
    """
    ctx = trip_context.load_context(bus.trip_id)
    if ctx is None:
        return None
    tz = trip_context.feed_timezone()
    trip_update = bus_matching_service.get_trip_update(bus.vehicle_id, bus.trip_id)
    timeline = stop_timeline(
        ctx, progress, gps_timestamp=bus.gps_timestamp, trip_update=trip_update,
        service_date=bus.trip_start_date, tz=tz,
    )
    from_index = ctx.find_index(stop_id=from_stop_id) if from_stop_id else None
    to_index = ctx.find_index(stop_id=to_stop_id) if to_stop_id else None

    stops = timeline
    if from_index is not None or to_index is not None:
        lo = from_index if from_index is not None else 0
        hi = to_index if to_index is not None else len(timeline) - 1
        stops = timeline[lo:hi + 1] if lo <= hi else []

    journey = None
    if to_index is not None:
        target = timeline[to_index]
        remaining = [s for s in timeline[: to_index + 1] if s["status"] in ("next", "upcoming")]
        journey = {
            "to_stop": {k: target[k] for k in ("stop_id", "stop_name", "stop_sequence", "latitude", "longitude")},
            "status": (
                "unknown" if target["status"] is None
                else "arrived" if target["status"] in ("current", "passed") else "on_the_way"
            ),
            "stops_remaining": len(remaining) if target["status"] is not None else None,
            "distance_km": target["distance_km"],
            "eta": target["eta"],
            "eta_source": target["eta_source"],
        }

    route = route_block(ctx)
    route["stops"] = timeline
    if include_path:
        route["path"] = path_points(ctx.polyline)
        along = (progress or {}).get("distance_along_m")
        end_m = ctx.stops[to_index].dist_m if to_index is not None else ctx.stops[-1].dist_m
        # Road still ahead of the bus (up to the rider's stop): what the map highlights.
        route["path_ahead"] = path_points(ctx.polyline, along, end_m) if along is not None and along < end_m else []
    return {"route": route, "stops": stops, "journey": journey}
