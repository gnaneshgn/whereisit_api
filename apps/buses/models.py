from django.db import models


class GpsStatus(models.TextChoices):
    LIVE = "LIVE", "Live"
    RECENT = "RECENT", "Recent"
    DELAYED = "DELAYED", "Delayed"
    UNAVAILABLE = "UNAVAILABLE", "Unavailable"


class LiveBus(models.Model):
    """
    Current state of one vehicle record from the APSRTC vehicle_positions feed.

    One row per upstream vehicle id, upserted on every sync. This is current
    state, NOT history: rows that drop out of the upstream full dataset are
    deleted on the next successful sync.

    Upstream identity facts (verified against the live feed, Nov 2026):
      * ``vehicle.vehicle.id`` is a per-trip-instance key such as
        "24112026_B002_1_AUTONAGAR VJA" and is unique within a snapshot.
      * The registration number is in ``licensePlate`` / ``label``; the same
        physical bus can appear under several ids (older trip records).
        The newest record per registration is the primary one; the others are
        flagged ``is_superseded``.
      * ``trip`` (tripId/routeId) is absent for roughly 18% of vehicles.
      * ``routeId`` is an internal slug, not the public route number.
    """

    vehicle_id = models.CharField(max_length=128, unique=True)
    registration_number = models.CharField(max_length=32, null=True, blank=True, db_index=True)

    trip_id = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    route_id = models.CharField(max_length=160, null=True, blank=True, db_index=True)
    trip_start_date = models.DateField(null=True, blank=True)
    # Labels and progress resolved from the imported GTFS at sync time. Null when unknown.
    route_number = models.CharField(max_length=32, null=True, blank=True, db_index=True)
    route_name = models.CharField(max_length=255, null=True, blank=True)
    start_point = models.CharField(max_length=255, null=True, blank=True)
    # GTFS trip_type of THIS bus's trip (e.g. "Metro Express", "City Ordinary"). Per trip, not per
    # route: one route can mix types (VZ13 runs both Metro Express and City Ordinary trips).
    bus_type = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    destination = models.CharField(max_length=255, null=True, blank=True)
    progress = models.JSONField(
        null=True, blank=True,
        help_text="Trip progress from apps.transit.services.progress_service (fresh GPS only)",
    )

    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    bearing = models.FloatField(null=True, blank=True, help_text="Degrees clockwise from north")
    speed = models.FloatField(null=True, blank=True, help_text="Metres per second (GTFS-RT unit)")

    gps_timestamp = models.DateTimeField(null=True, blank=True, db_index=True)
    gps_status = models.CharField(
        max_length=12, choices=GpsStatus.choices, default=GpsStatus.UNAVAILABLE, db_index=True
    )
    is_superseded = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True when a newer record exists for the same registration number",
    )
    last_synced_at = models.DateTimeField(db_index=True)
    raw_data = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["vehicle_id"]
        indexes = [
            models.Index(fields=["latitude", "longitude"], name="livebus_lat_lon_idx"),
            models.Index(fields=["route_id", "is_superseded"], name="livebus_route_primary_idx"),
        ]
        verbose_name = "live bus"
        verbose_name_plural = "live buses"

    def __str__(self):
        return f"{self.registration_number or '?'} ({self.vehicle_id})"

    @property
    def has_position(self):
        return self.latitude is not None and self.longitude is not None


class FeedSource(models.TextChoices):
    VEHICLE_POSITIONS = "vehicle_positions", "Vehicle positions"
    TRIP_UPDATES = "trip_updates", "Trip updates"
    STATUS = "status", "Feed status"


class SyncStatus(models.TextChoices):
    SUCCESS = "success", "Success"
    NOT_MODIFIED = "not_modified", "Not modified"
    SKIPPED = "skipped", "Skipped"
    REJECTED = "rejected", "Rejected (invalid payload)"
    FAILED = "failed", "Failed"


class FeedSyncLog(models.Model):
    """One row per synchronization attempt (pruned after a retention window)."""

    source = models.CharField(max_length=32, choices=FeedSource.choices, db_index=True)
    status = models.CharField(max_length=16, choices=SyncStatus.choices, db_index=True)
    total_vehicles = models.PositiveIntegerField(null=True, blank=True)
    skipped_records = models.PositiveIntegerField(null=True, blank=True)
    feed_timestamp = models.DateTimeField(
        null=True, blank=True, help_text="Upstream generation time (header.timestamp / feed_time)"
    )
    successful_sync_time = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["source", "status", "-created_at"], name="synclog_src_status_idx")]

    def __str__(self):
        return f"{self.source} {self.status} @ {self.created_at:%Y-%m-%d %H:%M:%S}"
