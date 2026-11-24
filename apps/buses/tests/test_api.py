import time

from django.test import override_settings
from django.urls import reverse

from apps.buses.models import GpsStatus
from apps.buses.services import sync_service

from .factories import FakeResponse, FeedTestCase, feed, patch_http, trip_updates_feed, vehicle_entity

VIZAG = dict(lat=17.75, lon=83.30)
VIJAYAWADA = dict(lat=16.51, lon=80.64)


class BusApiTests(FeedTestCase):
    def setUp(self):
        super().setUp()
        ts = self.ts = int(time.time())
        entities = [
            vehicle_entity(vehicle_id="LIVE1", plate="AP31Z0475", ts=ts - 10, **VIZAG),
            vehicle_entity(vehicle_id="RECENT1", plate="AP31Z0001", ts=ts - 200, route_id="r-2", **VIZAG),
            vehicle_entity(vehicle_id="OLD1", plate="AP16Z0001", ts=ts - 7200, **VIJAYAWADA),
            vehicle_entity(vehicle_id="NOPOS", plate="AP16Z0002", ts=ts - 5, lat=None, lon=None),
            vehicle_entity(vehicle_id="21112026_DUP", plate="AP31Z0475", ts=ts - 900, **VIZAG),
            vehicle_entity(vehicle_id="24112026_B002_2_AUTONAGAR VJA", plate="AP40Z0318", ts=ts - 30, **VIJAYAWADA),
        ]
        patcher, _ = patch_http({
            "vehicle_positions": FakeResponse(200, feed(entities, ts)),
            "trip_updates": FakeResponse(200, trip_updates_feed(ts, vehicle_id="LIVE1")),
        })
        with patcher:
            sync_service.sync_vehicle_positions()
            sync_service.sync_trip_updates()

    def ids(self, response):
        return [r["vehicle_id"] for r in response.json()["results"]]

    # ---- /api/buses/live/ -------------------------------------------------
    def test_live_returns_only_usable_primary_buses(self):
        response = self.client.get(reverse("buses:bus-live"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["feed_status"], "healthy")
        self.assertTrue(body["last_synced_at"].endswith("Z"))
        # OLD1 is UNAVAILABLE (2h old), NOPOS has no position, 21112026_DUP is superseded.
        self.assertEqual(set(self.ids(response)), {"LIVE1", "RECENT1", "24112026_B002_2_AUTONAGAR VJA"})

    def test_live_record_shape_and_no_fabrication(self):
        body = self.client.get(reverse("buses:bus-live"), {"status": "LIVE", "bbox": "17.6,83.0,17.95,83.45"}).json()
        self.assertEqual(body["count"], 1)
        bus = body["results"][0]
        self.assertEqual(bus["vehicle_id"], "LIVE1")
        self.assertEqual(bus["registration_number"], "AP31Z0475")
        self.assertEqual(bus["gps_status"], GpsStatus.LIVE)
        self.assertEqual(bus["speed_kmh"], 27.0)
        self.assertIsNone(bus["route_number"])
        self.assertIsNone(bus["current_stop"])
        self.assertIsNone(bus["destination"])
        self.assertNotIn("raw_data", bus)
        self.assertTrue(bus["gps_timestamp"].endswith("Z"))

    def test_live_status_filter_and_pagination(self):
        body = self.client.get(reverse("buses:bus-live"), {"status": "RECENT"}).json()
        self.assertEqual([r["vehicle_id"] for r in body["results"]], ["RECENT1"])
        body = self.client.get(reverse("buses:bus-live"), {"page_size": 1}).json()
        self.assertEqual((body["count"], body["total_pages"], len(body["results"])), (3, 3, 1))
        self.assertIsNotNone(body["next"])

    def test_live_route_filter(self):
        body = self.client.get(reverse("buses:bus-live"), {"route_id": "r-2"}).json()
        self.assertEqual([r["vehicle_id"] for r in body["results"]], ["RECENT1"])

    @override_settings(SERVICE_AREA_BBOX=(17.6, 83.0, 17.95, 83.45))
    def test_service_area_restricts_results(self):
        self.assertEqual(set(self.ids(self.client.get(reverse("buses:bus-live")))), {"LIVE1", "RECENT1"})

    def test_invalid_query_params_return_error_envelope(self):
        for params in ({"status": "FAST"}, {"bbox": "1,2,3"}, {"bbox": "18,83,17,84"}):
            with self.subTest(params=params):
                response = self.client.get(reverse("buses:bus-live"), params)
                self.assertEqual(response.status_code, 400)
                body = response.json()
                self.assertFalse(body["success"])
                self.assertEqual(body["error"]["code"], "invalid_input")

    # ---- /api/buses/ ------------------------------------------------------
    def test_all_buses_includes_unavailable_but_not_superseded_by_default(self):
        ids = set(self.ids(self.client.get(reverse("buses:bus-list"))))
        self.assertEqual(ids, {"LIVE1", "RECENT1", "OLD1", "NOPOS", "24112026_B002_2_AUTONAGAR VJA"})
        with_dups = self.client.get(reverse("buses:bus-list"), {"include_superseded": "true"}).json()
        self.assertEqual(with_dups["count"], 6)
        dup = next(r for r in with_dups["results"] if r["vehicle_id"] == "21112026_DUP")
        self.assertTrue(dup["is_superseded"])

    def test_unavailable_filter(self):
        body = self.client.get(reverse("buses:bus-list"), {"status": "UNAVAILABLE"}).json()
        self.assertEqual([r["vehicle_id"] for r in body["results"]], ["OLD1"])

    # ---- /api/buses/{id}/ -------------------------------------------------
    def test_detail_by_vehicle_id_with_spaces(self):
        response = self.client.get("/api/buses/24112026_B002_2_AUTONAGAR%20VJA/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["registration_number"], "AP40Z0318")

    def test_detail_by_registration_returns_newest_record(self):
        body = self.client.get("/api/buses/ap31z0475/").json()
        self.assertEqual(body["result"]["vehicle_id"], "LIVE1")
        self.assertFalse(body["result"]["is_superseded"])
        self.assertEqual(body["result"]["trip_update"]["next_stop"]["stop_id"], "237961")

    def test_detail_not_found(self):
        response = self.client.get("/api/buses/UNKNOWN999/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"success": False, "error": {"code": "not_found", "message": "No bus found for 'UNKNOWN999'."}})

    def test_unknown_url_returns_json_404(self):
        response = self.client.get("/api/does-not-exist/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    # ---- /api/buses/search/ ----------------------------------------------
    def test_search_by_registration_ignores_spacing_and_case(self):
        body = self.client.get(reverse("buses:bus-search"), {"q": "ap 31 z-0475"}).json()
        self.assertTrue(body["success"])
        # Superseded duplicate of the same plate is not returned.
        self.assertEqual([r["vehicle_id"] for r in body["results"]], ["LIVE1"])

    def test_search_partial_registration_exact_match_first(self):
        body = self.client.get(reverse("buses:bus-search"), {"q": "AP31Z"}).json()
        self.assertEqual({r["vehicle_id"] for r in body["results"]}, {"LIVE1", "RECENT1"})

    def test_search_by_vehicle_id_and_route_id(self):
        body = self.client.get(reverse("buses:bus-search"), {"q": "AUTONAGAR"}).json()
        self.assertEqual([r["vehicle_id"] for r in body["results"]], ["24112026_B002_2_AUTONAGAR VJA"])
        body = self.client.get(reverse("buses:bus-search"), {"q": "r-2"}).json()
        self.assertEqual([r["vehicle_id"] for r in body["results"]], ["RECENT1"])

    def test_search_requires_query(self):
        for params in ({}, {"q": "a"}):
            response = self.client.get(reverse("buses:bus-search"), params)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "invalid_input")

    def test_search_no_results(self):
        body = self.client.get(reverse("buses:bus-search"), {"q": "ZZ99"}).json()
        self.assertEqual((body["count"], body["results"]), (0, []))

    # ---- /api/buses/nearby/ ----------------------------------------------
    def test_nearby_buses_sorted_with_distance(self):
        body = self.client.get(
            reverse("buses:bus-nearby"), {"latitude": 17.7501, "longitude": 83.3001, "radius": 2}
        ).json()
        self.assertTrue(body["success"])
        self.assertEqual(body["radius_km"], 2)
        # Only usable, primary, in-radius buses (not OLD1 / NOPOS / superseded / Vijayawada ones).
        self.assertEqual({r["vehicle_id"] for r in body["results"]}, {"LIVE1", "RECENT1"})
        self.assertLess(body["results"][0]["distance_km"], 0.05)

    def test_nearby_respects_radius_and_limit(self):
        body = self.client.get(reverse("buses:bus-nearby"), {"latitude": 17.0, "longitude": 82.0, "radius": 1}).json()
        self.assertEqual(body["count"], 0)
        body = self.client.get(
            reverse("buses:bus-nearby"), {"latitude": 17.75, "longitude": 83.30, "limit": 1}
        ).json()
        self.assertEqual(body["count"], 1)

    def test_nearby_validation(self):
        for params in ({}, {"latitude": 95, "longitude": 83}, {"latitude": 17, "longitude": 83, "radius": 500}):
            with self.subTest(params=params):
                response = self.client.get(reverse("buses:bus-nearby"), params)
                self.assertEqual(response.status_code, 400)

    # ---- /api/routes/... --------------------------------------------------
    def test_route_buses_from_live_feed(self):
        body = self.client.get("/api/routes/r-2/buses/").json()
        self.assertEqual([r["vehicle_id"] for r in body["results"]], ["RECENT1"])

    def test_gtfs_endpoints_report_not_available(self):
        for url in ("/api/routes/", "/api/routes/search/?q=6A", "/api/routes/r-2/", "/api/routes/r-2/stops/",
                    "/api/stops/nearby/?latitude=17.7&longitude=83.2"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 501)
                self.assertEqual(response.json()["error"]["code"], "not_available")

    # ---- /api/feed/status/ -----------------------------------------------
    def test_feed_status(self):
        body = self.client.get(reverse("buses:feed-status")).json()
        self.assertTrue(body["success"])
        self.assertEqual(body["feed_status"], "healthy")
        vp = body["vehicle_positions"]
        self.assertEqual(vp["total_vehicles"], 6)
        self.assertEqual(vp["unique_buses"], 5)
        self.assertEqual(vp["gps_status_counts"], {"LIVE": 3, "RECENT": 1, "DELAYED": 0, "UNAVAILABLE": 1})
        self.assertEqual(body["trip_updates"]["total_trip_updates"], 1)
        self.assertIn("thresholds", body)

    def test_feed_status_reports_failure_while_serving_last_good_data(self):
        patcher, _ = patch_http({"vehicle_positions": FakeResponse(500, None)})
        with patcher:
            sync_service.sync_vehicle_positions()
        body = self.client.get(reverse("buses:feed-status")).json()
        self.assertEqual(body["feed_status"], "degraded")
        self.assertEqual(body["vehicle_positions"]["consecutive_failures"], 1)
        self.assertEqual(self.client.get(reverse("buses:bus-live")).json()["count"], 3)
