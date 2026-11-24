"""Thin HTTP layer: validate input, call selectors/services, serialize."""
from django.conf import settings
from django.core.cache import cache
from rest_framework import generics
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.buses import selectors
from apps.buses.serializers import (
    BusListQuerySerializer,
    BusRecordSerializer,
    LiveBusDetailSerializer,
    LiveBusSerializer,
    NearbyBusSerializer,
    NearbyQuerySerializer,
    SearchQuerySerializer,
)
from apps.buses.services import feed_status_service, sync_service
from common.utilities import iso_utc, utc_now

LIVE_RESPONSE_CACHE_SECONDS = 10


def feed_meta(now):
    return {
        "last_synced_at": iso_utc(feed_status_service.last_successful_sync()),
        "feed_status": feed_status_service.compute_feed_health(now),
    }


class BaseBusListView(generics.ListAPIView):
    serializer_class = LiveBusSerializer

    def get_filters(self):
        params = BusListQuerySerializer(data=self.request.query_params)
        params.is_valid(raise_exception=True)
        return params.validated_data

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["now"] = self.now
        return context

    def list(self, request, *args, **kwargs):
        self.now = utc_now()
        queryset = self.get_queryset()
        self.paginator.meta = feed_meta(self.now)
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(serializer.data)


class LiveBusListView(BaseBusListView):
    """
    GET /api/buses/live/

    One record per physical bus with a usable, non-expired GPS fix.
    Filters: status, route_id, bbox. Pagination: page, page_size.
    """

    def get_queryset(self):
        filters = self.get_filters()
        return selectors.live_buses(
            statuses=filters.get("status"), route_id=filters.get("route_id"), bbox=filters.get("bbox"), now=self.now
        )

    def list(self, request, *args, **kwargs):
        # The map polls this endpoint; responses are cached per data version + query
        # for a few seconds so GPS ages stay accurate while load stays flat.
        key = f"api:buses:live:v{sync_service.get_data_version()}:{request.get_full_path()}"
        cached = cache.get(key)
        if cached is not None:
            return Response(cached)
        response = super().list(request, *args, **kwargs)
        cache.set(key, response.data, timeout=LIVE_RESPONSE_CACHE_SECONDS)
        return response


class BusListView(BaseBusListView):
    """
    GET /api/buses/

    Every vehicle currently in the feed, including those without a recent fix
    (gps_status UNAVAILABLE). ``include_superseded=true`` also returns older
    duplicate records of the same registration.
    """

    serializer_class = BusRecordSerializer

    def get_queryset(self):
        filters = self.get_filters()
        return selectors.all_buses(
            statuses=filters.get("status"),
            route_id=filters.get("route_id"),
            bbox=filters.get("bbox"),
            include_superseded=filters.get("include_superseded", False),
            now=self.now,
        )


class BusSearchView(BaseBusListView):
    """
    GET /api/buses/search/?q=AP31Z0475

    Registration number (spaces/hyphens/case ignored), vehicle id, route id or
    route number. One record per physical bus, any GPS status.
    """

    def get_queryset(self):
        params = SearchQuerySerializer(data=self.request.query_params)
        params.is_valid(raise_exception=True)
        return selectors.search_buses(params.validated_data["q"], now=self.now)


class NearbyBusesView(APIView):
    """GET /api/buses/nearby/?latitude=17.7&longitude=83.2&radius=5&limit=50 (radius in km)."""

    def get(self, request):
        params = NearbyQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        p = params.validated_data
        now = utc_now()
        pairs = selectors.nearby_buses(p["latitude"], p["longitude"], p["radius"], limit=p["limit"], now=now)
        for bus, distance in pairs:
            bus.distance_km = distance
        data = NearbyBusSerializer([bus for bus, _ in pairs], many=True, context={"request": request, "now": now}).data
        return Response({
            "success": True,
            **feed_meta(now),
            "radius_km": p["radius"],
            "count": len(data),
            "results": data,
        })


class RouteBusesView(BaseBusListView):
    """
    GET /api/routes/{route_id}/buses/

    Live buses whose upstream routeId equals ``route_id``. Works before GTFS is
    imported because it only uses the live feed.
    """

    def get_queryset(self):
        return selectors.live_buses(route_id=self.kwargs["route_id"], now=self.now)


class NotAvailableView(APIView):
    """
    Placeholder for endpoints that need GTFS static data (Phase 2).
    Returns an explicit 501 so clients can tell "not built yet" apart from "not found".
    """

    def get(self, request, *args, **kwargs):
        return Response(
            {
                "success": False,
                "error": {
                    "code": "not_available",
                    "message": "Route and stop data is not available yet (GTFS import pending).",
                },
            },
            status=501,
        )


class BusDetailView(APIView):
    """GET /api/buses/{vehicle_id}/ (also accepts a registration number)."""

    def get(self, request, vehicle_id):
        now = utc_now()
        bus = selectors.get_bus(vehicle_id)
        if bus is None:
            raise NotFound(f"No bus found for '{vehicle_id}'.")
        data = LiveBusDetailSerializer(bus, context={"request": request, "now": now}).data
        return Response({"success": True, **feed_meta(now), "result": data})


class FeedStatusView(APIView):
    """GET /api/feed/status/"""

    def get(self, request):
        return Response({"success": True, **feed_status_service.get_feed_overview()})


class HealthView(APIView):
    """GET /api/health/ : lightweight liveness probe for load balancers."""

    throttle_classes = []

    def get(self, request):
        return Response({"success": True, "status": "ok", "debug": settings.DEBUG})
