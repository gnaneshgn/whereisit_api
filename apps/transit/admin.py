from django.contrib import admin

from apps.transit.models import GtfsFeed, Pattern, Route, Stop, Trip


class ReadOnlyAdmin(admin.ModelAdmin):
    """GTFS rows come only from import_gtfs."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(GtfsFeed)
class GtfsFeedAdmin(ReadOnlyAdmin):
    list_display = ("imported_at", "version", "is_active", "route_count", "stop_count", "trip_count",
                    "pattern_count", "approximate_pattern_count")


@admin.register(Route)
class RouteAdmin(ReadOnlyAdmin):
    list_display = ("short_name", "long_name", "service_type", "bus_type", "trip_count")
    list_filter = ("service_type", "bus_type")
    search_fields = ("route_id", "short_name", "long_name")


@admin.register(Stop)
class StopAdmin(ReadOnlyAdmin):
    list_display = ("name", "stop_id", "latitude", "longitude")
    search_fields = ("stop_id", "name")


@admin.register(Pattern)
class PatternAdmin(ReadOnlyAdmin):
    list_display = ("id", "route", "headsign", "stop_count", "trip_count", "length_m", "path_is_approximate")
    list_filter = ("path_is_approximate",)
    search_fields = ("route__route_id", "route__short_name", "headsign")


@admin.register(Trip)
class TripAdmin(ReadOnlyAdmin):
    list_display = ("trip_id", "route", "headsign", "bus_type", "start_time_s")
    search_fields = ("trip_id", "route__short_name")
