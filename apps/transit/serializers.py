"""Query-parameter validation and output shaping for the transit endpoints."""
from rest_framework import serializers

from apps.buses.services import location_service
from apps.transit.models import Route, ServiceType
from apps.transit.route_numbers import display_for


def route_dict(route: Route, active_buses=None) -> dict:
    """Matches the mobile normalizer: route_number / route_name / start_stop_name / end_stop_name."""
    return {
        "route_id": route.route_id,
        "route_number": display_for(route),
        "route_code": route.short_name or None,
        "route_name": route.long_name or None,
        "start_stop_name": route.origin_name or None,
        "end_stop_name": route.destination_name or None,
        "service_type": route.service_type,
        "bus_type": route.bus_type or None,
        "trips_per_day": route.trip_count,
        "active_buses": active_buses,
    }


def stop_dict(stop, *, distance_km=None, routes=None) -> dict:
    data = {
        "stop_id": stop.stop_id,
        "stop_name": stop.name,
        "latitude": stop.latitude,
        "longitude": stop.longitude,
    }
    if distance_km is not None:
        data["distance_km"] = round(distance_km, 3)
    if routes is not None:
        data["routes"] = routes
    return data


class RouteListQuerySerializer(serializers.Serializer):
    q = serializers.CharField(required=False, max_length=64, trim_whitespace=True)
    service_type = serializers.ChoiceField(required=False, choices=ServiceType.values)
    bbox = serializers.CharField(required=False, help_text="min_lat,min_lon,max_lat,max_lon")

    def validate_bbox(self, value):
        try:
            return location_service.parse_bbox(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class TextQuerySerializer(serializers.Serializer):
    q = serializers.CharField(min_length=2, max_length=64, trim_whitespace=True)


class RouteStopsQuerySerializer(serializers.Serializer):
    pattern_id = serializers.IntegerField(required=False, min_value=1)
    include_path = serializers.BooleanField(required=False, default=True)


class NearbyStopsQuerySerializer(serializers.Serializer):
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
    radius = serializers.FloatField(min_value=0.05, max_value=5, required=False, default=1, help_text="km")
    limit = serializers.IntegerField(min_value=1, max_value=100, required=False, default=20)


class JourneyQuerySerializer(serializers.Serializer):
    """Each end is a stop id or a coordinate (stops within walking distance are considered)."""

    from_stop = serializers.CharField(required=False, max_length=64)
    from_lat = serializers.FloatField(required=False, min_value=-90, max_value=90)
    from_lon = serializers.FloatField(required=False, min_value=-180, max_value=180)
    to_stop = serializers.CharField(required=False, max_length=64)
    to_lat = serializers.FloatField(required=False, min_value=-90, max_value=90)
    to_lon = serializers.FloatField(required=False, min_value=-180, max_value=180)
    include_path = serializers.BooleanField(required=False, default=False)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=20, default=10)

    def validate(self, attrs):
        errors = {}
        for end in ("from", "to"):
            has_stop = bool(attrs.get(f"{end}_stop"))
            has_coord = attrs.get(f"{end}_lat") is not None and attrs.get(f"{end}_lon") is not None
            if not has_stop and not has_coord:
                errors[end] = [f"Give {end}_stop or both {end}_lat and {end}_lon."]
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class StopBusesQuerySerializer(serializers.Serializer):
    """Stop by exact id, or every stop whose name contains ``q`` (e.g. "Simhachalam")."""

    q = serializers.CharField(required=False, min_length=2, max_length=64, trim_whitespace=True)
    stop_id = serializers.CharField(required=False, max_length=64)
    category = serializers.ChoiceField(required=False, choices=["metro", "local", "other"])

    def validate(self, attrs):
        if not attrs.get("q") and not attrs.get("stop_id"):
            raise serializers.ValidationError({"q": ["Give q (stop name) or stop_id."]})
        return attrs


class BusTripQuerySerializer(serializers.Serializer):
    from_stop = serializers.CharField(required=False, max_length=64)
    to_stop = serializers.CharField(required=False, max_length=64)
    include_path = serializers.BooleanField(required=False, default=True)
