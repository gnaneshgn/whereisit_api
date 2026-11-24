"""Read-side query helpers. Views call these; they never build querysets themselves."""
from __future__ import annotations

from django.db.models import Case, F, IntegerField, Q, QuerySet, Value, When

from apps.buses.models import LiveBus
from apps.buses.services import feed_status_service, location_service
from common.utilities import normalize_registration, utc_now

LIST_FIELDS = (
    "id", "vehicle_id", "registration_number", "trip_id", "route_id", "route_number", "destination",
    "latitude", "longitude", "bearing", "speed", "gps_timestamp", "is_superseded", "last_synced_at",
    "trip_start_date",
)


def _base() -> QuerySet:
    return location_service.apply_service_area(LiveBus.objects.only(*LIST_FIELDS))


def _apply_filters(qs: QuerySet, *, statuses=None, route_id=None, bbox=None, now=None) -> QuerySet:
    if statuses:
        now = now or utc_now()
        condition = Q()
        for status in statuses:
            condition |= feed_status_service.gps_status_q(status, now)
        qs = qs.filter(condition)
    if route_id:
        qs = qs.filter(route_id=route_id)
    return location_service.filter_bbox(qs, bbox)


def live_buses(*, statuses=None, route_id=None, bbox=None, now=None) -> QuerySet:
    """
    Buses to draw on the map: one record per physical bus, with a position,
    and a GPS fix that is not UNAVAILABLE. Freshest first.
    """
    now = now or utc_now()
    qs = _base().filter(is_superseded=False).filter(feed_status_service.available_gps_q(now))
    qs = _apply_filters(qs, statuses=statuses, route_id=route_id, bbox=bbox, now=now)
    return qs.order_by("-gps_timestamp", "vehicle_id")


def all_buses(*, statuses=None, route_id=None, bbox=None, include_superseded=False, now=None) -> QuerySet:
    """Every vehicle record currently in the feed, including ones without a usable fix."""
    qs = _base()
    if not include_superseded:
        qs = qs.filter(is_superseded=False)
    qs = _apply_filters(qs, statuses=statuses, route_id=route_id, bbox=bbox, now=now)
    return qs.order_by("registration_number", "vehicle_id")


def search_buses(query: str, *, now=None) -> QuerySet:
    """
    Match registration number (ignoring spaces/hyphens/case), upstream vehicle id,
    upstream route id or public route number. One record per physical bus;
    exact registration matches first, then freshest GPS.
    """
    text = query.strip()
    registration = normalize_registration(text)
    condition = Q(vehicle_id__icontains=text) | Q(route_id__icontains=text) | Q(route_number__iexact=text)
    if registration:
        condition |= Q(registration_number__icontains=registration)
    return (
        _base()
        .filter(is_superseded=False)
        .filter(condition)
        .annotate(
            exact_rank=Case(
                When(registration_number=registration, then=Value(0)),
                When(route_number__iexact=text, then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            )
        )
        .order_by("exact_rank", F("gps_timestamp").desc(nulls_last=True), "vehicle_id")
    )


def nearby_buses(latitude: float, longitude: float, radius_km: float, *, limit: int, now=None):
    """[(bus, distance_km)] with a usable GPS fix within ``radius_km``, nearest first."""
    now = now or utc_now()
    candidates = location_service.filter_bbox(
        _base().filter(is_superseded=False).filter(feed_status_service.available_gps_q(now)),
        location_service.bbox_around(latitude, longitude, radius_km),
    )
    return location_service.within_radius(candidates, latitude, longitude, radius_km, limit)


def get_bus(identifier: str) -> LiveBus | None:
    """
    Look up by upstream vehicle id; if none matches, treat ``identifier`` as a
    registration number and return the newest record for that bus.
    """
    bus = LiveBus.objects.filter(vehicle_id=identifier).first()
    if bus is not None:
        return bus
    registration = normalize_registration(identifier)
    if not registration:
        return None
    return (
        LiveBus.objects.filter(registration_number=registration)
        .order_by("is_superseded", "-gps_timestamp")
        .first()
    )
