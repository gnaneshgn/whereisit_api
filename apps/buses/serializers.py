from rest_framework import serializers

from apps.buses.models import GpsStatus, LiveBus
from apps.buses.services import bus_matching_service, feed_status_service, location_service
from common.utilities import utc_now

MS_TO_KMH = 3.6


class LiveBusSerializer(serializers.ModelSerializer):
    """
    Public representation of a bus. ``gps_status`` and ``gps_age_seconds`` are
    computed at response time from the vehicle's own GPS timestamp.
    Unknown values are null; nothing is inferred.
    """

    gps_status = serializers.SerializerMethodField()
    gps_age_seconds = serializers.SerializerMethodField()
    speed_kmh = serializers.SerializerMethodField()
    current_stop = serializers.SerializerMethodField()

    class Meta:
        model = LiveBus
        fields = [
            "vehicle_id", "registration_number", "route_id", "route_number", "trip_id",
            "latitude", "longitude", "bearing", "speed", "speed_kmh",
            "gps_timestamp", "gps_age_seconds", "gps_status", "current_stop", "destination",
        ]

    def _now(self):
        if "now" not in self.context:
            self.context["now"] = utc_now()
        return self.context["now"]

    def get_gps_status(self, obj):
        return feed_status_service.classify_gps_status(obj.gps_timestamp, self._now())

    def get_gps_age_seconds(self, obj):
        if obj.gps_timestamp is None:
            return None
        return max(0, int((self._now() - obj.gps_timestamp).total_seconds()))

    def get_speed_kmh(self, obj):
        return round(obj.speed * MS_TO_KMH, 1) if obj.speed is not None else None

    def get_current_stop(self, obj):
        # Requires GTFS stop geometry (Phase 2). Never guessed.
        return None


class BusRecordSerializer(LiveBusSerializer):
    """List view of every record, flagging older duplicates of the same bus."""

    class Meta(LiveBusSerializer.Meta):
        fields = LiveBusSerializer.Meta.fields + ["is_superseded"]


class LiveBusDetailSerializer(LiveBusSerializer):
    trip_update = serializers.SerializerMethodField()

    class Meta(LiveBusSerializer.Meta):
        fields = LiveBusSerializer.Meta.fields + [
            "trip_start_date", "is_superseded", "last_synced_at", "trip_update",
        ]

    def get_trip_update(self, obj):
        """Next-stop prediction from trip_updates.json (stop ids only until GTFS is imported)."""
        update = bus_matching_service.get_trip_update(obj.vehicle_id, obj.trip_id)
        if update is None:
            return None
        return {
            "next_stop": update.get("next_stop"),
            "remaining_stop_updates": update.get("remaining_stop_updates"),
            "updated_at": update.get("updated_at"),
        }


class NearbyBusSerializer(LiveBusSerializer):
    """Expects ``distance_km`` set on each instance by the view."""

    distance_km = serializers.SerializerMethodField()

    class Meta(LiveBusSerializer.Meta):
        fields = LiveBusSerializer.Meta.fields + ["distance_km"]

    def get_distance_km(self, obj):
        distance = getattr(obj, "distance_km", None)
        return round(distance, 3) if distance is not None else None


class SearchQuerySerializer(serializers.Serializer):
    q = serializers.CharField(min_length=2, max_length=64, trim_whitespace=True)


class NearbyQuerySerializer(serializers.Serializer):
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
    radius = serializers.FloatField(min_value=0.05, max_value=50, required=False, default=5, help_text="km")
    limit = serializers.IntegerField(min_value=1, max_value=200, required=False, default=50)


class BusListQuerySerializer(serializers.Serializer):
    """Validates query parameters for bus list endpoints."""

    status = serializers.CharField(required=False, help_text="Comma-separated: LIVE,RECENT,DELAYED,UNAVAILABLE")
    route_id = serializers.CharField(required=False, max_length=160)
    bbox = serializers.CharField(required=False, help_text="min_lat,min_lon,max_lat,max_lon")
    include_superseded = serializers.BooleanField(required=False, default=False)

    def validate_status(self, value):
        statuses = [s.strip().upper() for s in value.split(",") if s.strip()]
        invalid = [s for s in statuses if s not in GpsStatus.values]
        if invalid:
            raise serializers.ValidationError(
                f"Unknown status {', '.join(invalid)}. Allowed: {', '.join(GpsStatus.values)}"
            )
        return statuses

    def validate_bbox(self, value):
        try:
            return location_service.parse_bbox(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc
