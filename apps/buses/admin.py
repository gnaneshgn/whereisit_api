from django.contrib import admin

from apps.buses.models import FeedSyncLog, LiveBus


@admin.register(LiveBus)
class LiveBusAdmin(admin.ModelAdmin):
    list_display = (
        "registration_number", "vehicle_id", "route_id", "route_number", "gps_status",
        "gps_timestamp", "is_superseded", "last_synced_at",
    )
    list_filter = ("gps_status", "is_superseded")
    search_fields = ("registration_number", "vehicle_id", "route_id", "trip_id", "route_number")
    readonly_fields = [f.name for f in LiveBus._meta.fields]
    list_per_page = 100

    def has_add_permission(self, request):
        return False  # rows come only from the feed


@admin.register(FeedSyncLog)
class FeedSyncLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "source", "status", "total_vehicles", "feed_timestamp", "duration_ms")
    list_filter = ("source", "status")
    readonly_fields = [f.name for f in FeedSyncLog._meta.fields]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False
