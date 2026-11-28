"""
Route, stop, trip and journey endpoints (GTFS static + live progress).

Until ``import_gtfs`` has run they answer 501 ``not_available`` (same contract
as before), which the mobile app shows as "not available yet".
"""
from rest_framework import generics
from rest_framework.exceptions import APIException, NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.buses import selectors as bus_selectors
from apps.buses.serializers import usable_progress
from apps.buses.views import feed_meta
from apps.transit import selectors
from apps.transit.models import Route, Stop, Trip
from apps.transit.serializers import (
    BusTripQuerySerializer,
    JourneyQuerySerializer,
    NearbyStopsQuerySerializer,
    RouteListQuerySerializer,
    RouteStopsQuerySerializer,
    StopBusesQuerySerializer,
    TextQuerySerializer,
    route_dict,
    stop_dict,
)
from apps.transit.services import journey_service, stop_buses_service, timeline_service, trip_context
from common.utilities import iso_utc, utc_now


def _valid(serializer_class, data):
    params = serializer_class(data=data)
    params.is_valid(raise_exception=True)
    return params.validated_data


class GtfsNotAvailable(APIException):
    status_code = 501
    default_detail = "Route and stop data is not available yet (GTFS import pending)."
    default_code = "not_available"


class GtfsRequiredMixin:
    """Answer 501 not_available while no GTFS feed has been imported."""

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if trip_context.active_feed() is None:
            raise GtfsNotAvailable()


class RouteListView(GtfsRequiredMixin, generics.GenericAPIView):
    """
    GET /api/routes/?q=&service_type=city|intercity|other&bbox=

    Routes whose stops fall in the service area (SERVICE_AREA_BBOX, or ``bbox``).
    """

    def get_queryset(self):
        p = _valid(RouteListQuerySerializer, self.request.query_params)
        return selectors.routes(query=p.get("q"), service_type=p.get("service_type"), bbox=p.get("bbox"))

    def get(self, request):
        page = self.paginate_queryset(self.get_queryset())
        counts = selectors.active_bus_counts(r.route_id for r in page)
        return self.get_paginated_response([route_dict(r, counts.get(r.route_id, 0)) for r in page])


class RouteSearchView(RouteListView):
    """GET /api/routes/search/?q=  route code ("A009"), name, or a stop on the route ("Simhachalam")."""

    def get_queryset(self):
        return selectors.route_search(_valid(TextQuerySerializer, self.request.query_params)["q"])


def _route_or_404(route_id):
    route = Route.objects.filter(route_id=route_id).first()
    if route is None:
        raise NotFound(f"No route '{route_id}'.")
    return route


def _pattern_payload(route, pattern_id, include_path):
    patterns = selectors.route_patterns(route)
    if not patterns:
        raise NotFound(f"Route '{route.route_id}' has no stop data.")
    chosen = next((p for p in patterns if p.id == pattern_id), None) if pattern_id else patterns[0]
    if chosen is None:
        raise NotFound(f"Pattern {pattern_id} is not on route '{route.route_id}'.")
    stops = trip_context.pattern_stops(chosen.id)
    first = stops[0].dist_m
    payload = {
        "pattern_id": chosen.id,
        "towards": stops[-1].name,
        "headsign": chosen.headsign or None,
        "patterns": [selectors.pattern_summary(p) for p in patterns],
        "stops": [
            {**s.ref(), "latitude": s.latitude, "longitude": s.longitude,
             "distance_along_km": round((s.dist_m - first) / 1000.0, 2)}
            for s in stops
        ],
        "path_approximate": chosen.path_is_approximate,
    }
    if include_path:
        polyline, approximate = trip_context.pattern_polyline(chosen)
        payload["path"] = timeline_service.path_points(polyline)
        payload["path_approximate"] = approximate
    return payload


class RouteDetailView(GtfsRequiredMixin, APIView):
    """
    GET /api/routes/{route_id}/?pattern_id=

    Route info, its patterns (each direction / variant) and the chosen pattern's
    ordered stops and road path. Default pattern: the one with the most trips.
    """

    def get(self, request, route_id):
        route = _route_or_404(route_id)
        p = _valid(RouteStopsQuerySerializer, request.query_params)
        counts = selectors.active_bus_counts([route.route_id])
        body = route_dict(route, counts.get(route.route_id, 0))
        body.update(_pattern_payload(route, p.get("pattern_id"), p["include_path"]))
        return Response({"success": True, "result": body})


class RouteStopsView(GtfsRequiredMixin, APIView):
    """GET /api/routes/{route_id}/stops/?pattern_id=  ordered stops (results) + pattern list."""

    def get(self, request, route_id):
        route = _route_or_404(route_id)
        p = _valid(RouteStopsQuerySerializer, request.query_params)
        payload = _pattern_payload(route, p.get("pattern_id"), include_path=False)
        return Response({
            "success": True,
            "route_id": route.route_id,
            "pattern_id": payload["pattern_id"],
            "towards": payload["towards"],
            "headsign": payload["headsign"],
            "patterns": payload["patterns"],
            "count": len(payload["stops"]),
            "results": payload["stops"],
        })


class NearbyStopsView(GtfsRequiredMixin, APIView):
    """GET /api/stops/nearby/?latitude=&longitude=&radius=1&limit=20 (radius in km)."""

    def get(self, request):
        p = _valid(NearbyStopsQuerySerializer, request.query_params)
        pairs = journey_service.stops_near(p["latitude"], p["longitude"], p["radius"] * 1000.0, limit=p["limit"])
        routes = selectors.stops_with_routes([s for s, _ in pairs])
        results = [stop_dict(s, distance_km=d / 1000.0, routes=routes.get(s.stop_id, [])) for s, d in pairs]
        return Response({"success": True, "radius_km": p["radius"], "count": len(results), "results": results})


class StopSearchView(GtfsRequiredMixin, APIView):
    """GET /api/stops/search/?q=  stop names (exact and prefix matches first)."""

    def get(self, request):
        stops = selectors.search_stops(_valid(TextQuerySerializer, request.query_params)["q"])
        routes = selectors.stops_with_routes(stops)
        results = [stop_dict(s, routes=routes.get(s.stop_id, [])) for s in stops]
        return Response({"success": True, "count": len(results), "results": results})


def _stop_or_404(stop_id):
    stop = Stop.objects.filter(stop_id=stop_id).first()
    if stop is None:
        raise NotFound(f"No stop '{stop_id}'.")
    return stop


class StopBusesView(GtfsRequiredMixin, APIView):
    """
    GET /api/stops/buses/?q=Simhachalam            (every stop whose name contains q)
    GET /api/stops/buses/?stop_id=13801&category=metro

    Live buses whose trip starts at, ends at or passes through the stop(s), with
    approaching / at_stop / passed status, stops away and ETA, grouped by
    ``bus_category`` (metro | local | other). ``counts`` always covers all groups.
    """

    def get(self, request):
        p = _valid(StopBusesQuerySerializer, request.query_params)
        if p.get("stop_id"):
            stops = list(Stop.objects.filter(stop_id=p["stop_id"]))
            if not stops:
                raise NotFound(f"No stop '{p['stop_id']}'.")
        else:
            stops = selectors.search_stops(p["q"])
        now = utc_now()
        data = stop_buses_service.buses_via_stops(stops, now=now, category=p.get("category"))
        return Response({
            "success": True, **feed_meta(now),
            "stops": [stop_dict(s) for s in stops],
            "counts": data["counts"],
            "count": len(data["results"]),
            "results": data["results"],
        })


class StopDetailView(GtfsRequiredMixin, APIView):
    """GET /api/stops/{stop_id}/  stop with the routes serving it."""

    def get(self, request, stop_id):
        stop = _stop_or_404(stop_id)
        routes = selectors.stops_with_routes([stop]).get(stop.stop_id, [])
        return Response({"success": True, "result": stop_dict(stop, routes=routes)})


class StopArrivalsView(GtfsRequiredMixin, APIView):
    """GET /api/stops/{stop_id}/arrivals/  live buses heading to this stop, soonest first."""

    def get(self, request, stop_id):
        stop = _stop_or_404(stop_id)
        now = utc_now()
        results = journey_service.arrivals(stop)
        return Response({
            "success": True, **feed_meta(now),
            "stop": stop_dict(stop), "count": len(results), "results": results,
        })


class JourneyView(GtfsRequiredMixin, APIView):
    """
    GET /api/journeys/?from_stop=&to_stop=
    GET /api/journeys/?from_lat=&from_lon=&to_lat=&to_lon=   (stops within walking distance)

    Direct buses from source to destination: boarding/alighting stops, every
    stop in between, ride distance/time, live buses approaching with ETAs, and
    the next scheduled departures. ``include_path=true`` adds the road between
    the two stops (for drawing it, or for places along the way).
    """

    def get(self, request):
        p = _valid(JourneyQuerySerializer, request.query_params)
        origins = journey_service.resolve_endpoint(p.get("from_stop"), p.get("from_lat"), p.get("from_lon"))
        destinations = journey_service.resolve_endpoint(p.get("to_stop"), p.get("to_lat"), p.get("to_lon"))
        if origins is None:
            raise NotFound(f"No stop '{p.get('from_stop')}'.")
        if destinations is None:
            raise NotFound(f"No stop '{p.get('to_stop')}'.")
        now = utc_now()
        options = (
            journey_service.plan(origins, destinations, include_path=p["include_path"], limit=p["limit"])
            if origins and destinations else []
        )
        message = None
        if not origins or not destinations:
            message = "No bus stop within walking distance of the {}.".format("start" if not origins else "destination")
        elif not options:
            message = "No direct bus connects these places. Journeys with a change are not supported yet."
        return Response({
            "success": True, **feed_meta(now),
            "from_candidates": [stop_dict(s, distance_km=w / 1000.0) for s, w in (origins or [])],
            "to_candidates": [stop_dict(s, distance_km=w / 1000.0) for s, w in (destinations or [])],
            "count": len(options),
            "message": message,
            "options": options,
        })


class TripDetailView(GtfsRequiredMixin, APIView):
    """GET /api/trips/{trip_id}/  scheduled stops (local service-day times) and path."""

    def get(self, request, trip_id):
        trip = Trip.objects.filter(trip_id=trip_id).select_related("route", "pattern").first()
        ctx = trip_context.build_context(trip) if trip else None
        if ctx is None:
            raise NotFound(f"No trip '{trip_id}'.")
        stops = []
        for s in ctx.stops:
            offset = ctx.offset(s.index)
            secs = None if offset is None or trip.start_time_s is None else trip.start_time_s + offset
            stops.append({
                **s.ref(), "latitude": s.latitude, "longitude": s.longitude,
                "distance_along_km": round((s.dist_m - ctx.stops[0].dist_m) / 1000.0, 2),
                # GTFS service-day time; may exceed 24:00 for trips running past midnight.
                "scheduled_time": None if secs is None else f"{secs // 3600:02d}:{secs % 3600 // 60:02d}",
            })
        body = {
            "trip_id": trip.trip_id, **timeline_service.route_block(ctx),
            "stops": stops, "path": timeline_service.path_points(ctx.polyline),
        }
        return Response({"success": True, "result": body})


class BusTripView(GtfsRequiredMixin, APIView):
    """
    GET /api/buses/{vehicle_id}/trip/?from_stop=&to_stop=

    Live timeline of the bus's trip: every stop with passed/current/next/upcoming,
    distance and ETA, the full path and ``path_ahead`` (road still to travel).
    With ``to_stop`` it also returns ``journey``: stops left, distance and ETA to
    the rider's stop; ``from_stop`` / ``to_stop`` trim ``stops`` to that section.
    """

    def get(self, request, vehicle_id):
        p = _valid(BusTripQuerySerializer, request.query_params)
        bus = bus_selectors.get_bus(vehicle_id)
        if bus is None:
            raise NotFound(f"No bus found for '{vehicle_id}'.")
        now = utc_now()
        progress = usable_progress(bus, now)
        data = timeline_service.bus_timeline(
            bus, progress, from_stop_id=p.get("from_stop"), to_stop_id=p.get("to_stop"),
            include_path=p["include_path"],
        )
        if data is None:
            raise NotFound("Route information unavailable for this bus (its trip is not in the GTFS data).")
        if p.get("to_stop") and data["journey"] is None:
            raise NotFound(f"Stop '{p['to_stop']}' is not on this bus's trip.")
        return Response({
            "success": True, **feed_meta(now),
            "vehicle_id": bus.vehicle_id,
            "registration_number": bus.registration_number,
            "trip_id": bus.trip_id,
            "gps_timestamp": iso_utc(bus.gps_timestamp),
            "progress": progress,
            **data,
        })
