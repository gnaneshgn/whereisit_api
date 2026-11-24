"""
Vehicle identity and route/trip mapping.

* Registration de-duplication: upstream can list one physical bus under
  several vehicle ids (older trip records stay in the feed for a while).
  The record with the newest GPS fix is primary; the rest are superseded.
* Route mapping: upstream ``routeId`` is an internal slug
  (e.g. "b011-rtc-complex-vsp-simhachalam"), not the public route number.
  Public numbers and destinations come from GTFS static data (Phase 2).
  Until GTFS is imported they stay None and are never guessed.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone as dt_timezone

from django.core.cache import cache

from apps.buses.services.apsrtc_service import NormalizedVehicle

KEY_TRIP_UPDATES = "feed:trip_updates:by_vehicle"

_EPOCH = datetime.min.replace(tzinfo=dt_timezone.utc)


def _primary_rank(vehicle: NormalizedVehicle):
    return (
        vehicle.gps_timestamp or _EPOCH,
        vehicle.latitude is not None,
        vehicle.trip_id is not None,
        vehicle.vehicle_id,  # deterministic final tie-break
    )


def find_superseded_ids(vehicles: list[NormalizedVehicle]) -> set[str]:
    """Vehicle ids that are older duplicates of another record with the same registration."""
    by_registration: dict[str, list[NormalizedVehicle]] = defaultdict(list)
    for vehicle in vehicles:
        if vehicle.registration_number:
            by_registration[vehicle.registration_number].append(vehicle)

    superseded: set[str] = set()
    for group in by_registration.values():
        if len(group) < 2:
            continue
        primary = max(group, key=_primary_rank)
        superseded.update(v.vehicle_id for v in group if v.vehicle_id != primary.vehicle_id)
    return superseded


def lookup_route_metadata(route_ids: set[str], trip_ids: set[str]) -> tuple[dict, dict]:
    """
    Map upstream ids to public-facing labels.

    Returns ``({route_id: route_short_name}, {trip_id: trip_headsign})``.
    Phase 1 has no GTFS tables yet, so both maps are empty and route_number /
    destination remain null. Phase 2 resolves them from BusRoute / BusTrip.
    """
    return {}, {}


# ---------------------------------------------------------------------------
# Trip updates (cached map keyed by vehicle id)
# ---------------------------------------------------------------------------
def store_trip_updates(updates_by_vehicle: dict[str, dict]) -> None:
    cache.set(KEY_TRIP_UPDATES, updates_by_vehicle, timeout=None)


def get_trip_update(vehicle_id: str, trip_id: str | None = None) -> dict | None:
    """Latest trip update for a vehicle, only if it refers to the same trip."""
    updates = cache.get(KEY_TRIP_UPDATES) or {}
    update = updates.get(vehicle_id)
    if update is None:
        return None
    if trip_id and update.get("trip_id") and update["trip_id"] != trip_id:
        return None
    return update
