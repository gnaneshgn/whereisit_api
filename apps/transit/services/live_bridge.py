"""
Live feed <-> GTFS join, run inside every vehicle-positions sync.

For each vehicle it resolves public labels (route code, route name, origin,
destination) and computes trip progress. Progress is only computed for fresh
GPS (LIVE / RECENT): an older fix would place the bus at a stop it has long left.
"""
from __future__ import annotations

import logging

from apps.buses.models import GpsStatus
from apps.buses.services import feed_status_service
from apps.transit.models import Route
from apps.transit.services import trip_context
from apps.transit.services.progress_service import compute_progress

logger = logging.getLogger(__name__)

PROGRESS_STATUSES = {GpsStatus.LIVE, GpsStatus.RECENT}


def _route_labels(route_ids) -> dict:
    labels = {}
    ids = [r for r in set(route_ids) if r]
    for start in range(0, len(ids), 500):
        for route in Route.objects.filter(route_id__in=ids[start:start + 500]).only(
            "route_id", "short_name", "long_name", "origin_name", "destination_name"
        ):
            labels[route.route_id] = route
    return labels


def enrich_vehicles(vehicles, *, previous_progress: dict, trip_updates: dict, now) -> dict:
    """
    {vehicle_id: {"route_number", "route_name", "start_point", "destination", "progress"}}
    Vehicles without GTFS data are absent (their fields stay null).
    """
    if trip_context.active_feed() is None:
        return {}
    contexts = trip_context.load_contexts(v.trip_id for v in vehicles)
    routes = _route_labels(v.route_id for v in vehicles)
    result = {}
    for v in vehicles:
        ctx = contexts.get(v.trip_id)
        route = ctx.route if ctx else routes.get(v.route_id)
        if route is None:
            continue
        info = {
            "route_number": route.short_name or None,
            "route_name": route.long_name or None,
            "start_point": None,
            "destination": None,
            "progress": None,
        }
        if ctx is not None:
            # The trip's real last stop. trip_headsign is NOT used: in this feed ~24% of
            # headsigns name neither end of the trip (e.g. an Airport-bound trip signed
            # "Tagarapuvalasa"), which would send riders the wrong way.
            info["start_point"] = ctx.stops[0].name
            info["destination"] = ctx.stops[-1].name
            status = feed_status_service.classify_gps_status(v.gps_timestamp, now)
            if v.latitude is not None and status in PROGRESS_STATUSES:
                prev = previous_progress.get(v.vehicle_id) or {}
                prev_along = prev.get("distance_along_m") if prev.get("pattern_id") == ctx.trip.pattern_id else None
                try:
                    info["progress"] = compute_progress(
                        ctx, v.latitude, v.longitude, bearing=v.bearing, speed=v.speed,
                        gps_timestamp=v.gps_timestamp, prev_along=prev_along,
                        trip_update=trip_updates.get(v.vehicle_id),
                    )
                except Exception:  # one bad shape must never break the sync
                    logger.exception("progress failed for %s (trip %s)", v.vehicle_id, v.trip_id)
        # Route known but trip unknown: a route runs both ways, so origin/destination stay null.
        result[v.vehicle_id] = info
    return result
