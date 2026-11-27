"""Read-side helpers for routes and stops. Views call these; they never build querysets themselves."""
from __future__ import annotations

from collections import Counter, defaultdict

from django.conf import settings
from django.db.models import Q, QuerySet

from apps.buses import selectors as bus_selectors
from apps.transit.models import Pattern, PatternStop, Route, Stop
from apps.transit.services import trip_context


def _in_area(qs: QuerySet, bbox) -> QuerySet:
    """Routes whose stop bounding box intersects ``bbox``."""
    if not bbox:
        return qs
    min_lat, min_lon, max_lat, max_lon = bbox
    return qs.filter(min_lat__lte=max_lat, max_lat__gte=min_lat, min_lon__lte=max_lon, max_lon__gte=min_lon)


def routes(*, query=None, service_type=None, bbox=None) -> QuerySet:
    qs = _in_area(Route.objects.all(), bbox or settings.SERVICE_AREA_BBOX)
    if service_type:
        qs = qs.filter(service_type=service_type)
    if query:
        text = query.strip()
        qs = qs.filter(
            Q(short_name__iexact=text) | Q(short_name__istartswith=text) | Q(long_name__icontains=text)
            | Q(route_id__icontains=text)
        )
    return qs.order_by("short_name", "route_id")


def route_search(query: str) -> QuerySet:
    """Route code / name, or any route serving a stop whose name matches."""
    text = query.strip()
    stop_route_ids = PatternStop.objects.filter(stop__name__icontains=text).values("pattern__route_id")
    qs = _in_area(Route.objects.all(), settings.SERVICE_AREA_BBOX).filter(
        Q(short_name__iexact=text) | Q(short_name__istartswith=text) | Q(long_name__icontains=text)
        | Q(route_id__in=stop_route_ids)
    )
    return qs.order_by("short_name", "route_id")


def active_bus_counts(route_ids) -> Counter:
    """Live buses per route (one per physical bus, usable GPS)."""
    ids = list(route_ids)
    if not ids:
        return Counter()
    return Counter(bus_selectors.live_buses().filter(route_id__in=ids).values_list("route_id", flat=True))


def route_patterns(route: Route) -> list[Pattern]:
    return list(Pattern.objects.filter(route=route).order_by("-trip_count", "id"))


def stops_with_routes(stops) -> dict:
    """{stop_id: [{route_id, route_number, route_name}]} sorted by route code."""
    ids = [s.stop_id for s in stops]
    out = defaultdict(dict)
    rows = PatternStop.objects.filter(stop_id__in=ids).values_list(
        "stop_id", "pattern__route_id", "pattern__route__short_name", "pattern__route__long_name"
    ).distinct()
    for sid, rid, short, long_name in rows:
        out[sid][rid] = {"route_id": rid, "route_number": short or None, "route_name": long_name or None}
    return {sid: sorted(v.values(), key=lambda r: (r["route_number"] or "", r["route_id"])) for sid, v in out.items()}


def search_stops(query: str, limit: int = 30) -> list[Stop]:
    text = query.strip()
    qs = Stop.objects.filter(name__icontains=text)
    bbox = settings.SERVICE_AREA_BBOX
    if bbox:
        qs = qs.filter(latitude__gte=bbox[0], longitude__gte=bbox[1], latitude__lte=bbox[2], longitude__lte=bbox[3])
    # Exact / prefix matches first, then alphabetical
    stops = list(qs[:500])
    lowered = text.lower()
    stops.sort(key=lambda s: (s.name.lower() != lowered, not s.name.lower().startswith(lowered), s.name))
    return stops[:limit]


def pattern_summary(pattern: Pattern) -> dict:
    stops = trip_context.pattern_stops(pattern.id)
    return {
        "pattern_id": pattern.id,
        "towards": stops[-1].name if stops else None,
        "headsign": pattern.headsign or None,
        "start_stop_name": stops[0].name if stops else None,
        "end_stop_name": stops[-1].name if stops else None,
        "stop_count": pattern.stop_count,
        "trip_count": pattern.trip_count,
        "length_km": round(pattern.length_m / 1000.0, 2),
        "path_approximate": pattern.path_is_approximate,
    }
