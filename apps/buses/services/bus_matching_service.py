"""
Vehicle identity and route/trip mapping.

* Registration de-duplication: upstream can list one physical bus under
  several vehicle ids (older trip records stay in the feed for a while).
  The record with the newest GPS fix is primary; the rest are superseded.
* Route mapping: upstream ``routeId`` is an internal slug
  (e.g. "b011-rtc-complex-vsp-simhachalam"), not the public route number.
  Route codes, destinations and trip progress come from the imported GTFS
  (``apps.transit.services.live_bridge``). Without GTFS they stay None.
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


# ---------------------------------------------------------------------------
# Trip updates (cached map keyed by vehicle id)
# ---------------------------------------------------------------------------
def store_trip_updates(updates_by_vehicle: dict[str, dict]) -> None:
    cache.set(KEY_TRIP_UPDATES, updates_by_vehicle, timeout=None)


def get_trip_updates_map() -> dict[str, dict]:
    """The whole cached map; read it once when handling many vehicles."""
    return cache.get(KEY_TRIP_UPDATES) or {}


def get_trip_update(vehicle_id: str, trip_id: str | None = None) -> dict | None:
    """Latest trip update for a vehicle, only if it refers to the same trip."""
    update = get_trip_updates_map().get(vehicle_id)
    if update is None:
        return None
    if trip_id and update.get("trip_id") and update["trip_id"] != trip_id:
        return None
    return update
