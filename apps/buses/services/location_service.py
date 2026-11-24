"""Geographic helpers: distances, bounding boxes and area filtering."""
from __future__ import annotations

import math

from django.conf import settings
from django.db.models import QuerySet

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bbox_around(latitude: float, longitude: float, radius_km: float):
    """Bounding box enclosing a circle; used as a cheap indexed pre-filter before haversine."""
    d_lat = math.degrees(radius_km / EARTH_RADIUS_KM)
    cos_lat = max(math.cos(math.radians(latitude)), 1e-6)
    d_lon = math.degrees(radius_km / (EARTH_RADIUS_KM * cos_lat))
    return (
        max(-90.0, latitude - d_lat),
        max(-180.0, longitude - d_lon),
        min(90.0, latitude + d_lat),
        min(180.0, longitude + d_lon),
    )


def within_radius(items, latitude: float, longitude: float, radius_km: float, limit: int):
    """[(item, distance_km)] for items within radius, nearest first, at most ``limit``."""
    results = []
    for item in items:
        if item.latitude is None or item.longitude is None:
            continue
        distance = haversine_km(latitude, longitude, item.latitude, item.longitude)
        if distance <= radius_km:
            results.append((item, distance))
    results.sort(key=lambda pair: pair[1])
    return results[:limit]


def parse_bbox(raw: str):
    """'min_lat,min_lon,max_lat,max_lon' -> tuple of floats. Raises ValueError."""
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must have 4 comma-separated numbers: min_lat,min_lon,max_lat,max_lon")
    min_lat, min_lon, max_lat, max_lon = (float(p) for p in parts)
    if not (-90 <= min_lat < max_lat <= 90 and -180 <= min_lon < max_lon <= 180):
        raise ValueError("bbox values out of range or min >= max")
    return min_lat, min_lon, max_lat, max_lon


def filter_bbox(queryset: QuerySet, bbox) -> QuerySet:
    if not bbox:
        return queryset
    min_lat, min_lon, max_lat, max_lon = bbox
    return queryset.filter(
        latitude__gte=min_lat, latitude__lte=max_lat, longitude__gte=min_lon, longitude__lte=max_lon
    )


def apply_service_area(queryset: QuerySet) -> QuerySet:
    """Restrict to SERVICE_AREA_BBOX when configured (e.g. Visakhapatnam only)."""
    return filter_bbox(queryset, settings.SERVICE_AREA_BBOX)
