"""
APSRTC GTFS-Realtime (JSON) client: fetch, validate, parse and normalize.

Observed upstream format (maps.reclaim.city, inspected Nov 2026)::

    {"header": {"gtfsRealtimeVersion": "2.0", "incrementality": "FULL_DATASET",
                "timestamp": "1795527490"},                       # string seconds
     "entity": [
       {"id": "24112026_9123_2_VISAKHAPATNAM",
        "vehicle": {
          "trip": {"tripId": "...", "startDate": "20261124", "routeId": "b011-..."},  # optional
          "position": {"latitude": 17.74, "longitude": 83.24, "bearing": 108.0,
                       "speed": 9.22},                              # speed in m/s
          "timestamp": "1795527458",                                # string seconds
          "vehicle": {"id": "24112026_9123_2_VISAKHAPATNAM",
                      "label": "AP30Z0225", "licensePlate": "AP30Z0225"}}}]}

Quirks handled here: missing ``trip``; empty or partial plates (e.g. "39Z0142");
vehicle timestamps years in the future; duplicate plates across entities.
Nothing is invented: unknown values become ``None``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from common.utilities import (
    clean_str,
    epoch_to_datetime,
    is_valid_coordinate,
    iso_utc,
    normalize_registration,
    to_float,
    to_int,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class FeedError(Exception):
    """Base class for upstream feed problems."""


class FeedUnavailableError(FeedError):
    """Network failure, timeout or non-success HTTP status."""


class FeedInvalidError(FeedError):
    """Response arrived but is not usable (bad JSON, wrong structure, empty)."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class NormalizedVehicle:
    vehicle_id: str
    registration_number: str | None
    trip_id: str | None
    route_id: str | None
    trip_start_date: date | None
    latitude: float | None
    longitude: float | None
    bearing: float | None
    speed: float | None
    gps_timestamp: datetime | None
    raw: dict | None = None

    def to_cache_dict(self) -> dict:
        data = asdict(self)
        data.pop("raw", None)
        data["trip_start_date"] = self.trip_start_date.isoformat() if self.trip_start_date else None
        data["gps_timestamp"] = self.gps_timestamp.isoformat() if self.gps_timestamp else None
        return data


@dataclass
class ParseStats:
    total_entities: int = 0
    skipped_no_vehicle: int = 0
    skipped_no_id: int = 0
    missing_position: int = 0
    missing_trip: int = 0
    missing_registration: int = 0
    invalid_timestamp: int = 0
    duplicate_ids: int = 0

    @property
    def skipped(self) -> int:
        return self.skipped_no_vehicle + self.skipped_no_id + self.duplicate_ids


@dataclass
class VehicleSnapshot:
    feed_timestamp: datetime | None
    vehicles: list[NormalizedVehicle]
    stats: ParseStats = field(default_factory=ParseStats)


@dataclass
class FetchResult:
    payload: Any | None
    etag: str | None
    not_modified: bool = False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _cfg(key):
    return settings.APSRTC[key]


def build_session() -> requests.Session:
    retries = Retry(
        total=_cfg("MAX_RETRIES"),
        connect=_cfg("MAX_RETRIES"),
        read=_cfg("MAX_RETRIES"),
        status=_cfg("MAX_RETRIES"),
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": _cfg("USER_AGENT"), "Accept": "application/json"})
    return session


def fetch_json(url: str, etag: str | None = None, session: requests.Session | None = None) -> FetchResult:
    """GET ``url`` and decode JSON. Raises FeedUnavailableError / FeedInvalidError."""
    session = session or build_session()
    headers = {"If-None-Match": etag} if etag else {}
    try:
        response = session.get(
            url, headers=headers, timeout=(_cfg("CONNECT_TIMEOUT"), _cfg("READ_TIMEOUT"))
        )
    except requests.RequestException as exc:
        raise FeedUnavailableError(f"Request to {url} failed: {exc.__class__.__name__}: {exc}") from exc

    if response.status_code == 304:
        return FetchResult(payload=None, etag=etag, not_modified=True)
    if response.status_code != 200:
        raise FeedUnavailableError(f"{url} returned HTTP {response.status_code}")

    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise FeedInvalidError(f"{url} returned invalid JSON: {exc}") from exc

    return FetchResult(payload=payload, etag=response.headers.get("ETag"))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _validate_feed_message(payload) -> list:
    if not isinstance(payload, dict):
        raise FeedInvalidError(f"Expected a JSON object, got {type(payload).__name__}")
    entities = payload.get("entity")
    if entities is None:
        raise FeedInvalidError("Feed has no 'entity' list")
    if not isinstance(entities, list):
        raise FeedInvalidError("'entity' is not a list")
    return entities


def _parse_header_timestamp(payload) -> datetime | None:
    header = payload.get("header") if isinstance(payload, dict) else None
    if not isinstance(header, dict):
        return None
    return epoch_to_datetime(header.get("timestamp"))


def _parse_start_date(value) -> date | None:
    text = clean_str(value)
    if not text or len(text) != 8 or not text.isdigit():
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def parse_vehicle_entity(entity, now: datetime, stats: ParseStats, keep_raw: bool = False):
    """Normalize one FeedEntity. Returns None when the record cannot be identified."""
    if not isinstance(entity, dict) or not isinstance(entity.get("vehicle"), dict):
        stats.skipped_no_vehicle += 1
        return None

    vp = entity["vehicle"]
    descriptor = _dict(vp.get("vehicle"))
    trip = _dict(vp.get("trip"))
    position = _dict(vp.get("position"))

    vehicle_id = clean_str(descriptor.get("id")) or clean_str(entity.get("id"))
    if not vehicle_id:
        stats.skipped_no_id += 1
        return None

    registration = normalize_registration(descriptor.get("licensePlate")) or normalize_registration(
        descriptor.get("label")
    )
    if not registration:
        stats.missing_registration += 1

    trip_id = clean_str(trip.get("tripId"))
    route_id = clean_str(trip.get("routeId"))
    if not trip_id and not route_id:
        stats.missing_trip += 1

    latitude = to_float(position.get("latitude"))
    longitude = to_float(position.get("longitude"))
    if not is_valid_coordinate(latitude, longitude):
        latitude = longitude = None
        stats.missing_position += 1

    bearing = to_float(position.get("bearing"))
    if bearing is not None and not 0 <= bearing <= 360:
        bearing = None
    speed = to_float(position.get("speed"))
    if speed is not None and speed < 0:
        speed = None

    gps_timestamp = epoch_to_datetime(vp.get("timestamp"))
    future_limit = now + timedelta(seconds=settings.GPS_FRESHNESS["FUTURE_TOLERANCE"])
    if vp.get("timestamp") is not None and (gps_timestamp is None or gps_timestamp > future_limit):
        # Upstream sometimes reports timestamps years ahead; they are not trustworthy.
        stats.invalid_timestamp += 1
        gps_timestamp = None

    return NormalizedVehicle(
        vehicle_id=vehicle_id[:128],
        registration_number=registration[:32] if registration else None,
        trip_id=trip_id[:128] if trip_id else None,
        route_id=route_id[:160] if route_id else None,
        trip_start_date=_parse_start_date(trip.get("startDate")),
        latitude=latitude,
        longitude=longitude,
        bearing=bearing,
        speed=speed,
        gps_timestamp=gps_timestamp,
        raw=entity if keep_raw else None,
    )


def parse_vehicle_positions(payload, now: datetime, keep_raw: bool = False) -> VehicleSnapshot:
    entities = _validate_feed_message(payload)
    stats = ParseStats(total_entities=len(entities))
    vehicles: dict[str, NormalizedVehicle] = {}

    for entity in entities:
        vehicle = parse_vehicle_entity(entity, now, stats, keep_raw=keep_raw)
        if vehicle is None:
            continue
        existing = vehicles.get(vehicle.vehicle_id)
        if existing is not None:
            stats.duplicate_ids += 1
            # Keep whichever record carries the newer GPS fix.
            if (vehicle.gps_timestamp or datetime.min.replace(tzinfo=now.tzinfo)) <= (
                existing.gps_timestamp or datetime.min.replace(tzinfo=now.tzinfo)
            ):
                continue
        vehicles[vehicle.vehicle_id] = vehicle

    return VehicleSnapshot(
        feed_timestamp=_parse_header_timestamp(payload), vehicles=list(vehicles.values()), stats=stats
    )


def parse_trip_updates(payload) -> tuple[datetime | None, dict[str, dict]]:
    """
    Compact TripUpdate entities into ``{vehicle_id: {...}}``.

    Only the first stopTimeUpdate (the next stop still ahead) is kept: that is
    all the app needs and keeps the cached map small.
    """
    entities = _validate_feed_message(payload)
    result: dict[str, dict] = {}
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        tu = _dict(entity.get("tripUpdate"))
        if not tu:
            continue
        trip = _dict(tu.get("trip"))
        vehicle_id = clean_str(_dict(tu.get("vehicle")).get("id"))
        if not vehicle_id:
            continue

        next_stop = None
        updates = tu.get("stopTimeUpdate")
        if isinstance(updates, list) and updates and isinstance(updates[0], dict):
            first = updates[0]
            event = _dict(first.get("arrival")) or _dict(first.get("departure"))
            arrival = epoch_to_datetime(event.get("time"))
            next_stop = {
                "stop_id": clean_str(first.get("stopId")),
                "stop_sequence": to_int(first.get("stopSequence")),
                "expected_arrival": iso_utc(arrival),
                "delay_seconds": to_int(event.get("delay")),
            }

        updated = epoch_to_datetime(tu.get("timestamp"))
        result[vehicle_id] = {
            "trip_id": clean_str(trip.get("tripId")),
            "route_id": clean_str(trip.get("routeId")),
            "next_stop": next_stop,
            "remaining_stop_updates": len(updates) if isinstance(updates, list) else 0,
            "updated_at": iso_utc(updated),
        }
    return _parse_header_timestamp(payload), result


def parse_status(payload) -> dict:
    """status.json: {"network", "feed_time", "written", "vehicles", "on_trip", "on_route", "trip_updates"}."""
    if not isinstance(payload, dict):
        raise FeedInvalidError("status.json is not a JSON object")
    feed_time = epoch_to_datetime(payload.get("feed_time"))
    written = epoch_to_datetime(payload.get("written"))
    return {
        "network": clean_str(payload.get("network")),
        "feed_time": feed_time.isoformat() if feed_time else None,
        "written": written.isoformat() if written else None,
        "vehicles": to_int(payload.get("vehicles")),
        "on_trip": to_int(payload.get("on_trip")),
        "on_route": to_int(payload.get("on_route")),
        "trip_updates": to_int(payload.get("trip_updates")),
    }


# ---------------------------------------------------------------------------
# High-level fetchers
# ---------------------------------------------------------------------------
def fetch_vehicle_positions(now: datetime, etag: str | None = None):
    """Returns (FetchResult, VehicleSnapshot | None). Snapshot is None on 304."""
    result = fetch_json(_cfg("VEHICLE_POSITIONS_URL"), etag=etag)
    if result.not_modified:
        return result, None
    snapshot = parse_vehicle_positions(result.payload, now, keep_raw=_cfg("STORE_RAW_VEHICLE_DATA"))
    return result, snapshot


def fetch_trip_updates(etag: str | None = None):
    result = fetch_json(_cfg("TRIP_UPDATES_URL"), etag=etag)
    if result.not_modified:
        return result, None, None
    feed_timestamp, updates = parse_trip_updates(result.payload)
    return result, feed_timestamp, updates


def fetch_status() -> dict:
    return parse_status(fetch_json(_cfg("STATUS_URL")).payload)
