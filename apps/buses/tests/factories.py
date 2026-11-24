"""Test helpers: payloads shaped exactly like the real APSRTC feed, and a fake HTTP layer."""
import json
from unittest import mock

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings

LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def vehicle_entity(
    vehicle_id="24112026_A033_11_WALTAIR",
    plate="AP27Z0208",
    trip_id="A033_11_WALTAIR",
    route_id="a033-bheemunipatnam-railway-station-vsp",
    lat=17.75187,
    lon=83.3499,
    ts=None,
    bearing=210.4,
    speed=7.5,
    start_date="20261124",
):
    vp = {
        "position": {"latitude": lat, "longitude": lon, "bearing": bearing, "speed": speed},
        "vehicle": {"id": vehicle_id, "label": plate, "licensePlate": plate},
    }
    if ts is not None:
        vp["timestamp"] = str(ts)
    if trip_id or route_id:
        vp["trip"] = {"startDate": start_date}
        if trip_id:
            vp["trip"]["tripId"] = trip_id
        if route_id:
            vp["trip"]["routeId"] = route_id
    return {"id": vehicle_id, "vehicle": vp}


def feed(entities, header_ts):
    return {
        "header": {"gtfsRealtimeVersion": "2.0", "incrementality": "FULL_DATASET", "timestamp": str(header_ts)},
        "entity": entities,
    }


def trip_updates_feed(header_ts, vehicle_id="24112026_A033_11_WALTAIR", trip_id="A033_11_WALTAIR"):
    return feed(
        [
            {
                "id": f"tu-{vehicle_id}",
                "tripUpdate": {
                    "trip": {"tripId": trip_id, "startDate": "20261124", "routeId": "a033-x"},
                    "stopTimeUpdate": [
                        {"stopSequence": 13, "arrival": {"delay": 60, "time": str(header_ts + 600)}, "stopId": "237961"},
                        {"stopSequence": 14, "arrival": {"delay": 60, "time": str(header_ts + 900)}, "stopId": "237962"},
                    ],
                    "vehicle": {"id": vehicle_id, "label": "AP27Z0208"},
                    "timestamp": str(header_ts),
                },
            }
        ],
        header_ts,
    )


def status_payload(feed_ts, vehicles=2):
    return {"network": "apsrtc", "feed_time": feed_ts, "written": feed_ts, "vehicles": vehicles,
            "on_trip": vehicles, "on_route": vehicles, "trip_updates": 1}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self._text = text
        self.headers = headers or {}

    def json(self):
        if self._text is not None:
            return json.loads(self._text)
        return self._payload


class FakeSession:
    """Routes URLs to queued responses or exceptions."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers or {}))
        for key, value in self.routes.items():
            if key in url:
                result = value.pop(0) if isinstance(value, list) else value
                if isinstance(result, Exception):
                    raise result
                return result
        raise requests.ConnectionError(f"no fake route for {url}")


def patch_http(routes):
    session = FakeSession(routes)
    return mock.patch("apps.buses.services.apsrtc_service.build_session", return_value=session), session


@override_settings(CACHES=LOCMEM_CACHE, REST_FRAMEWORK={
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PAGINATION_CLASS": "common.pagination.StandardPagination",
    "PAGE_SIZE": 100,
    "EXCEPTION_HANDLER": "common.exceptions.api_exception_handler",
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_THROTTLE_CLASSES": [],
    "UNAUTHENTICATED_USER": None,
})
class FeedTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
