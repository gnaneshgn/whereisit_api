"""
GTFS static data (routes, stops, trips, shapes), imported by ``manage.py import_gtfs``.

Source: the reclaim.city APSRTC GTFS (``data/apsrtc``). It is published by the
same project as the realtime feed, so live ``trip_id`` / ``route_id`` values
join directly (verified: 100% of live trips match, Nov 2026).

Storage is normalised around *patterns*: a pattern is one exact ordered stop
list on one shape. ~55k trips collapse into ~12k patterns, so each stop's
distance along the road is computed and stored once per pattern rather than
once per trip. Trip-specific timings live on ``Trip.arrival_offsets``.
"""
from django.db import models


class GtfsFeed(models.Model):
    """One row per import. Only the newest ``is_active`` row is in use."""

    version = models.CharField(max_length=64, blank=True, default="")
    publisher = models.CharField(max_length=255, blank=True, default="")
    timezone = models.CharField(max_length=64, default="Asia/Kolkata")
    source = models.CharField(max_length=500, blank=True, default="")
    service_start = models.DateField(null=True, blank=True)
    service_end = models.DateField(null=True, blank=True)
    route_count = models.PositiveIntegerField(default=0)
    stop_count = models.PositiveIntegerField(default=0)
    trip_count = models.PositiveIntegerField(default=0)
    pattern_count = models.PositiveIntegerField(default=0)
    shape_count = models.PositiveIntegerField(default=0)
    approximate_pattern_count = models.PositiveIntegerField(
        default=0, help_text="Patterns without a usable shape; their path joins the stops"
    )
    is_active = models.BooleanField(default=True, db_index=True)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-imported_at"]

    def __str__(self):
        return f"GTFS {self.version or '?'} ({self.imported_at:%Y-%m-%d %H:%M})"


class Stop(models.Model):
    stop_id = models.CharField(max_length=64, primary_key=True)
    name = models.CharField(max_length=255, db_index=True)
    latitude = models.FloatField()
    longitude = models.FloatField()

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["latitude", "longitude"], name="stop_lat_lon_idx")]

    def __str__(self):
        return f"{self.name} ({self.stop_id})"


class ServiceType(models.TextChoices):
    CITY = "city", "City"
    INTERCITY = "intercity", "Intercity"
    OTHER = "other", "Other"


class Route(models.Model):
    """
    ``route_id`` is the upstream slug (e.g. "a009-maddilapalem-rtc-complex-vsp").
    ``short_name`` is the APSRTC route code (e.g. "A009"); it is the only route
    number the data provides. One route can run in both directions.
    """

    route_id = models.CharField(max_length=160, primary_key=True)
    short_name = models.CharField(max_length=32, db_index=True, blank=True, default="")
    long_name = models.CharField(max_length=255, blank=True, default="")
    description = models.CharField(max_length=255, blank=True, default="")
    service_type = models.CharField(max_length=16, choices=ServiceType.choices, default=ServiceType.OTHER, db_index=True)
    bus_type = models.CharField(max_length=64, blank=True, default="", help_text="Most common trip_type, e.g. City Ordinary")
    origin_name = models.CharField(max_length=255, blank=True, default="")
    destination_name = models.CharField(max_length=255, blank=True, default="")
    trip_count = models.PositiveIntegerField(default=0)
    # Bounding box of every stop served, used for service-area filtering.
    min_lat = models.FloatField(null=True, blank=True)
    min_lon = models.FloatField(null=True, blank=True)
    max_lat = models.FloatField(null=True, blank=True)
    max_lon = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["short_name", "route_id"]
        indexes = [models.Index(fields=["min_lat", "max_lat", "min_lon", "max_lon"], name="route_bbox_idx")]

    def __str__(self):
        return f"{self.short_name} {self.long_name}".strip()


class Shape(models.Model):
    """Road geometry as a Google encoded polyline (precision 5, ~1 m)."""

    shape_id = models.CharField(max_length=64, primary_key=True)
    encoded = models.TextField()
    point_count = models.PositiveIntegerField()
    length_m = models.FloatField()

    def __str__(self):
        return self.shape_id


class Pattern(models.Model):
    """One exact ordered stop list on one shape, shared by many trips."""

    id = models.PositiveIntegerField(primary_key=True)
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="patterns")
    shape = models.ForeignKey(Shape, on_delete=models.SET_NULL, null=True, blank=True, related_name="patterns")
    headsign = models.CharField(max_length=255, blank=True, default="")
    stop_count = models.PositiveSmallIntegerField()
    trip_count = models.PositiveIntegerField(default=0)
    length_m = models.FloatField(help_text="Along-shape distance from the first to the last stop")
    path_is_approximate = models.BooleanField(
        default=False, help_text="True when the shape was missing or did not fit the stops"
    )

    class Meta:
        ordering = ["route_id", "-trip_count"]

    def __str__(self):
        return f"{self.route_id} → {self.headsign} ({self.stop_count} stops)"


class PatternStop(models.Model):
    pattern = models.ForeignKey(Pattern, on_delete=models.CASCADE, related_name="pattern_stops")
    index = models.PositiveSmallIntegerField(help_text="0-based position within the pattern")
    stop_sequence = models.PositiveIntegerField(help_text="GTFS stop_sequence (matches trip updates)")
    stop = models.ForeignKey(Stop, on_delete=models.CASCADE, related_name="pattern_stops")
    dist_m = models.FloatField(help_text="Distance along the pattern's path")
    snap_offset_m = models.FloatField(help_text="How far the stop is from the path")

    class Meta:
        ordering = ["pattern_id", "index"]
        constraints = [models.UniqueConstraint(fields=["pattern", "index"], name="patternstop_unique_index")]
        indexes = [models.Index(fields=["stop", "pattern"], name="patternstop_stop_idx")]


class Trip(models.Model):
    trip_id = models.CharField(max_length=128, primary_key=True)
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="trips")
    pattern = models.ForeignKey(Pattern, on_delete=models.CASCADE, related_name="trips")
    headsign = models.CharField(max_length=255, blank=True, default="")
    short_name = models.CharField(max_length=64, blank=True, default="")
    bus_type = models.CharField(max_length=64, blank=True, default="")
    start_time_s = models.PositiveIntegerField(
        null=True, blank=True, help_text="Scheduled time at the first stop, seconds after service-day midnight (may exceed 24h)"
    )
    # Seconds from the first stop, one per pattern stop. Null entries = no timing in the source.
    arrival_offsets = models.JSONField(default=list)

    class Meta:
        ordering = ["trip_id"]
        indexes = [models.Index(fields=["pattern", "start_time_s"], name="trip_pattern_start_idx")]

    def __str__(self):
        return self.trip_id
