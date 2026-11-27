from rest_framework import serializers

from apps.buses.models import GpsStatus, LiveBus
from apps.buses.services import bus_matching_service, feed_status_service, location_service
from common.utilities import utc_now

MS_TO_KMH = 3.6
PROGRESS_GPS_STATUSES = {GpsStatus.LIVE, GpsStatus.RECENT}


def usable_progress(bus, now):
    """Stored progress, only while the GPS fix it was computed from is still fresh."""
    if not bus.progress:
        return None
    if feed_status_service.classify_gps_status(bus.gps_timestamp, now) not in PROGRESS_GPS_STATUSES:
        return None
    return bus.progress


class LiveBusSerializer(serializers.ModelSerializer):
    """
    Public representation of a bus. ``gps_status`` and ``gps_age_seconds`` are
    computed at response time from the vehicle's own GPS timestamp.
    Stop progress comes from the imported GTFS and is null when unknown,
    off route, or when the GPS fix is no longer fresh. Nothing is inferred.
    """

    gps_status = serializers.SerializerMethodField()
    gps_age_seconds = serializers.SerializerMethodField()
    speed_kmh = serializers.SerializerMethodField()
    current_stop = serializers.SerializerMethodField()
    next_stop = serializers.SerializerMethodField()
    last_stop = serializers.SerializerMethodField()
    current_location = serializers.SerializerMethodField()
    trip_progress = serializers.SerializerMethodField()

    class Meta:
        model = LiveBus
        fields = [
            "vehicle_id", "registration_number", "route_id", "route_number", "route_name", "trip_id",
            "start_point", "destination",
            "latitude", "longitude", "bearing", "speed", "speed_kmh",
            "gps_timestamp", "gps_age_seconds", "gps_status",
            "current_stop", "next_stop", "last_stop", "current_location", "trip_progress",
        ]

    def _now(self):
        if "now" not in self.context:
            self.context["now"] = utc_now()
        return self.context["now"]

    def _progress(self, obj):
        return usable_progress(obj, self._now())

    def get_gps_status(self, obj):
        return feed_status_service.classify_gps_status(obj.gps_timestamp, self._now())

    def get_gps_age_seconds(self, obj):
        if obj.gps_timestamp is None:
            return None
        return max(0, int((self._now() - obj.gps_timestamp).total_seconds()))

    def get_speed_kmh(self, obj):
        return round(obj.speed * MS_TO_KMH, 1) if obj.speed is not None else None

    def get_current_stop(self, obj):
        """The stop the bus is at (within TRANSIT.AT_STOP_M), else null."""
        return (self._progress(obj) or {}).get("current_stop")

    def get_next_stop(self, obj):
        return (self._progress(obj) or {}).get("next_stop")

    def get_last_stop(self, obj):
        return (self._progress(obj) or {}).get("last_stop")

    def get_current_location(self, obj):
        progress = self._progress(obj)
        if not progress:
            return None
        current, last, nxt = progress.get("current_stop"), progress.get("last_stop"), progress.get("next_stop")
        if current:
            return f"At {current['stop_name']}"
        if last and nxt:
            return f"Between {last['stop_name']} and {nxt['stop_name']}"
        if nxt:
            return f"Approaching {nxt['stop_name']}"
        return None

    def get_trip_progress(self, obj):
        progress = self._progress(obj)
        if not progress:
            return None
        return {
            key: progress.get(key)
            for key in ("status", "progress_pct", "stops_remaining", "distance_along_m", "route_length_m",
                        "path_approximate", "as_of")
        }


class BusRecordSerializer(LiveBusSerializer):
    """List view of every record, flagging older duplicates of the same bus."""

    class Meta(LiveBusSerializer.Meta):
        fields = LiveBusSerializer.Meta.fields + ["is_superseded"]


class LiveBusDetailSerializer(LiveBusSerializer):
    """
    Adds the trip timeline: ``upcoming_stops`` (next first, with distance and
    ETA) and ``route`` (every stop with passed/current/next/upcoming status and
    the road path). Built from the bus's own trip, so direction is exact.
    """

    trip_update = serializers.SerializerMethodField()
    upcoming_stops = serializers.SerializerMethodField()
    route = serializers.SerializerMethodField()

    class Meta(LiveBusSerializer.Meta):
        fields = LiveBusSerializer.Meta.fields + [
            "trip_start_date", "is_superseded", "last_synced_at", "trip_update", "upcoming_stops", "route",
        ]

    def _timeline(self, obj):
        key = f"_timeline:{obj.vehicle_id}"
        if key not in self.context:
            from apps.transit.services import timeline_service  # transit depends on buses

            self.context[key] = timeline_service.bus_timeline(obj, self._progress(obj))
        return self.context[key]

    def get_upcoming_stops(self, obj):
        data = self._timeline(obj)
        if not data:
            return []
        return [s for s in data["stops"] if s["status"] in ("next", "upcoming")]

    def get_route(self, obj):
        data = self._timeline(obj)
        return data["route"] if data else None

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
