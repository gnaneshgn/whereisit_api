import time
from datetime import timedelta

from apps.buses.services import sync_service
from apps.buses.tests.factories import FakeResponse, FeedTestCase, feed, patch_http, vehicle_entity
from apps.transit.models import Pattern, PatternStop, Trip
from apps.transit.services import trip_context
from apps.transit.services.gtfs_import import import_gtfs
from apps.transit.services.progress_service import compute_progress, parse_iso
from apps.transit.tests.fixtures import LAT, STOP_LAT, GtfsFolder, lon_at
from common.utilities import utc_now

ROUTE = "a009-stop-1-stop-4"


def trip_update_entity(vehicle_id, trip_id, stop_id, seq, eta_ts, updated_ts):
    return {
        "id": f"tu-{vehicle_id}",
        "tripUpdate": {
            "trip": {"tripId": trip_id, "startDate": "20261124", "routeId": ROUTE},
            "stopTimeUpdate": [{"stopSequence": seq, "stopId": stop_id, "arrival": {"time": str(eta_ts), "delay": 60}}],
            "vehicle": {"id": vehicle_id},
            "timestamp": str(updated_ts),
        },
    }


class GtfsTestCase(FeedTestCase):
    def setUp(self):
        super().setUp()
        with GtfsFolder() as folder:
            self.stats = import_gtfs(folder)
        trip_context.reset_feed_memo()


class ImportTests(GtfsTestCase):
    def test_counts_and_patterns(self):
        s = self.stats
        self.assertEqual((s.routes, s.stops, s.trips, s.patterns), (2, 4, 4, 3))
        self.assertEqual(s.approximate_patterns, 1)  # T_NOSHAPE references a missing shape
        self.assertEqual(s.interpolated_times, 1)    # T_EAST2 has no time at S3
        # Both eastbound trips share one pattern
        self.assertEqual(Trip.objects.get(trip_id="T_EAST").pattern_id, Trip.objects.get(trip_id="T_EAST2").pattern_id)

    def test_stops_are_placed_along_the_road(self):
        east = Trip.objects.get(trip_id="T_EAST").pattern
        dists = list(PatternStop.objects.filter(pattern=east).order_by("index").values_list("dist_m", flat=True))
        for got, expected in zip(dists, (0, 1000, 2000, 3000)):
            self.assertAlmostEqual(got, expected, delta=5)
        offsets = PatternStop.objects.filter(pattern=east).values_list("snap_offset_m", flat=True)
        self.assertTrue(all(abs(o - 15) < 1 for o in offsets))

    def test_interpolated_time_and_offsets(self):
        trip = Trip.objects.get(trip_id="T_EAST2")
        self.assertEqual(trip.start_time_s, 9 * 3600)
        self.assertEqual(trip.arrival_offsets, [0, 300, 600, 900])

    def test_route_labels(self):
        pattern = Pattern.objects.get(trips__trip_id="T_WEST")
        self.assertEqual(pattern.route.short_name, "A009")
        self.assertEqual((pattern.route.origin_name, pattern.route.destination_name), ("Stop 1", "Stop 4"))
        self.assertEqual(pattern.route.service_type, "city")
        self.assertEqual(pattern.route.bus_type, "City Ordinary")

    def test_reimport_replaces_data(self):
        with GtfsFolder() as folder:
            import_gtfs(folder)
        self.assertEqual(Trip.objects.count(), 4)


class ProgressTests(GtfsTestCase):
    def ctx(self, trip_id):
        return trip_context.load_context(trip_id)

    def test_in_transit_eastbound(self):
        gps = utc_now()
        p = compute_progress(self.ctx("T_EAST"), LAT, lon_at(1.5), bearing=90, speed=8, gps_timestamp=gps)
        self.assertEqual(p["status"], "in_transit")
        self.assertEqual(p["last_stop"]["stop_id"], "S2")
        self.assertEqual(p["next_stop"]["stop_id"], "S3")
        self.assertAlmostEqual(p["next_stop"]["distance_m"], 500, delta=5)
        self.assertEqual(p["stops_remaining"], 2)
        self.assertAlmostEqual(p["progress_pct"], 50, delta=0.5)
        # Schedule says 7.5 min at 1.5 km and 10 min at S3 -> 150 s away
        eta = parse_iso(p["next_stop"]["expected_arrival"])
        self.assertAlmostEqual((eta - gps).total_seconds(), 150, delta=2)
        self.assertEqual(p["next_stop"]["eta_source"], "schedule")

    def test_same_place_opposite_direction_uses_its_own_trip(self):
        p = compute_progress(self.ctx("T_WEST"), LAT, lon_at(1.5), bearing=270, speed=8, gps_timestamp=utc_now())
        self.assertEqual((p["last_stop"]["stop_id"], p["next_stop"]["stop_id"]), ("S3", "S2"))

    def test_at_stop(self):
        p = compute_progress(self.ctx("T_EAST"), STOP_LAT, lon_at(1.02), gps_timestamp=utc_now())
        self.assertEqual(p["status"], "at_stop")
        self.assertEqual(p["current_stop"]["stop_id"], "S2")
        self.assertEqual(p["next_stop"]["stop_id"], "S3")

    def test_start_and_end(self):
        start = compute_progress(self.ctx("T_EAST"), LAT, lon_at(0), gps_timestamp=utc_now())
        self.assertEqual(start["status"], "not_started")
        end = compute_progress(self.ctx("T_EAST"), LAT, lon_at(3), gps_timestamp=utc_now())
        self.assertEqual(end["status"], "completed")
        self.assertIsNone(end["next_stop"])

    def test_off_route_reports_no_stop(self):
        p = compute_progress(self.ctx("T_EAST"), LAT + 0.01, lon_at(1), gps_timestamp=utc_now())
        self.assertEqual(p["status"], "off_route")
        self.assertIsNone(p["next_stop"])
        self.assertIsNone(p["last_stop"])

    def test_realtime_prediction_preferred_and_propagated(self):
        now = utc_now()
        update = {
            "trip_id": "T_EAST", "updated_at": now.isoformat(),
            "next_stop": {"stop_id": "S2", "stop_sequence": 2, "expected_arrival": (now + timedelta(seconds=200)).isoformat()},
        }
        p = compute_progress(self.ctx("T_EAST"), LAT, lon_at(0.5), gps_timestamp=now, trip_update=update)
        self.assertEqual(p["next_stop"]["eta_source"], "realtime")
        self.assertAlmostEqual((parse_iso(p["next_stop"]["expected_arrival"]) - now).total_seconds(), 200, delta=2)

    def test_approximate_path_without_shape(self):
        ctx = self.ctx("T_NOSHAPE")
        self.assertTrue(ctx.approximate)
        p = compute_progress(ctx, LAT, lon_at(1), gps_timestamp=utc_now())
        self.assertTrue(p["path_approximate"])
        self.assertEqual(p["next_stop"]["stop_id"], "S3")


class LiveApiTests(GtfsTestCase):
    """Full pipeline: GTFS import -> live sync (mocked HTTP) -> API."""

    def setUp(self):
        super().setUp()
        ts = self.ts = int(time.time())
        on_road = dict(lat=LAT, speed=8.0)
        vehicles = [
            vehicle_entity("V_EAST", "AP31Z0001", "T_EAST", ROUTE, lon=lon_at(0.5), ts=ts - 10, bearing=90, **on_road),
            vehicle_entity("V_WEST", "AP31Z0002", "T_WEST", ROUTE, lon=lon_at(1.5), ts=ts - 10, bearing=270, **on_road),
            vehicle_entity("V_OFF", "AP31Z0003", "T_EAST2", ROUTE, lat=LAT + 0.01, lon=lon_at(1), ts=ts - 10),
            vehicle_entity("V_OLD", "AP31Z0004", "T_EAST", ROUTE, lon=lon_at(2.5), lat=LAT, ts=ts - 1000),
            vehicle_entity("V_NOGTFS", "AP31Z0005", "UNKNOWN_1", "unknown-route", lat=LAT, lon=lon_at(1), ts=ts - 10),
        ]
        updates = feed([trip_update_entity("V_EAST", "T_EAST", "S2", 2, ts + 120, ts - 5)], ts)
        patcher, _ = patch_http({
            "trip_updates": FakeResponse(200, updates),
            "vehicle_positions": FakeResponse(200, feed(vehicles, ts)),
        })
        with patcher:
            sync_service.sync_trip_updates()
            sync_service.sync_vehicle_positions()

    def live(self):
        return {b["vehicle_id"]: b for b in self.client.get("/api/buses/live/").json()["results"]}

    # ---- labels + progress on the live list ------------------------------
    def test_live_list_has_route_labels_and_stops(self):
        buses = self.live()
        east = buses["V_EAST"]
        self.assertEqual((east["route_number"], east["route_name"]), ("A009", "Stop 1 – Stop 4"))
        self.assertEqual((east["start_point"], east["destination"]), ("Stop 1", "Stop 4"))
        self.assertEqual(east["last_stop"]["stop_id"], "S1")
        self.assertEqual(east["next_stop"]["stop_id"], "S2")
        self.assertEqual(east["next_stop"]["eta_source"], "realtime")
        self.assertEqual(east["current_location"], "Between Stop 1 and Stop 2")
        self.assertEqual(east["trip_progress"]["status"], "in_transit")
        # bus_type comes from each bus's own trip, not the route
        self.assertEqual(east["bus_type"], "City Ordinary")
        self.assertEqual(buses["V_OFF"]["bus_type"], "Metro Express")  # T_EAST2, same route
        self.assertIsNone(buses["V_NOGTFS"]["bus_type"])
        west = buses["V_WEST"]
        self.assertEqual(west["next_stop"]["stop_id"], "S2")
        # Destination is the trip's real last stop, not its (misleading) headsign "Far Town"
        self.assertEqual((west["start_point"], west["destination"]), ("Stop 4", "Stop 1"))

    def test_off_route_stale_and_unknown_buses_are_never_guessed(self):
        buses = self.live()
        off = buses["V_OFF"]
        self.assertEqual(off["trip_progress"]["status"], "off_route")
        self.assertIsNone(off["next_stop"])
        self.assertIsNone(off["current_location"])
        old = buses["V_OLD"]  # DELAYED GPS: labels yes, stop progress no
        self.assertEqual(old["route_number"], "A009")
        self.assertIsNone(old["trip_progress"])
        self.assertIsNone(old["next_stop"])
        unknown = buses["V_NOGTFS"]
        self.assertIsNone(unknown["route_number"])
        self.assertIsNone(unknown["destination"])

    def test_search_by_route_code_and_place(self):
        ids = lambda q: {b["vehicle_id"] for b in self.client.get("/api/buses/search/", {"q": q}).json()["results"]}
        self.assertIn("V_EAST", ids("A009"))
        self.assertIn("V_EAST", ids("Stop 4"))

    # ---- bus detail + live trip timeline -------------------------------------
    def test_bus_detail_upcoming_stops_and_route(self):
        body = self.client.get("/api/buses/V_EAST/").json()["result"]
        upcoming = body["upcoming_stops"]
        self.assertEqual([s["stop_id"] for s in upcoming], ["S2", "S3", "S4"])
        self.assertEqual(upcoming[0]["status"], "next")
        self.assertAlmostEqual(upcoming[0]["distance_km"], 0.5, delta=0.01)
        # realtime anchor at S2 (+120 s) propagated with the schedule (+300 s per stop)
        s2, s3 = (parse_iso(s["eta"]) for s in upcoming[:2])
        self.assertAlmostEqual((s3 - s2).total_seconds(), 300, delta=2)
        route = body["route"]
        self.assertEqual(route["route_number"], "A009")
        self.assertEqual([s["status"] for s in route["stops"]], ["passed", "next", "upcoming", "upcoming"])
        self.assertGreaterEqual(len(route["path"]), 2)

    def test_bus_trip_journey_to_my_stop(self):
        body = self.client.get("/api/buses/V_EAST/trip/", {"to_stop": "S3"}).json()
        journey = body["journey"]
        self.assertEqual(journey["status"], "on_the_way")
        self.assertEqual(journey["stops_remaining"], 2)
        self.assertAlmostEqual(journey["distance_km"], 1.5, delta=0.01)
        self.assertIsNotNone(journey["eta"])
        self.assertEqual([s["stop_id"] for s in body["stops"]], ["S1", "S2", "S3"])
        ahead = body["route"]["path_ahead"]
        self.assertAlmostEqual(ahead[0][1], lon_at(0.5), places=4)
        self.assertAlmostEqual(ahead[-1][1], lon_at(2), places=4)

        passed = self.client.get("/api/buses/V_EAST/trip/", {"to_stop": "S1"}).json()["journey"]
        self.assertEqual((passed["status"], passed["stops_remaining"]), ("arrived", 0))
        self.assertEqual(self.client.get("/api/buses/V_EAST/trip/", {"to_stop": "NOPE"}).status_code, 404)
        self.assertEqual(self.client.get("/api/buses/V_NOGTFS/trip/").status_code, 404)

    # ---- journeys --------------------------------------------------------
    def test_journey_finds_only_the_right_direction_with_live_bus(self):
        body = self.client.get("/api/journeys/", {"from_stop": "S2", "to_stop": "S4", "include_path": "true"}).json()
        self.assertEqual(body["count"], 1)
        option = body["options"][0]
        self.assertEqual(option["route"]["route_number"], "A009")
        self.assertEqual((option["board"]["stop_id"], option["alight"]["stop_id"]), ("S2", "S4"))
        self.assertEqual([s["stop_id"] for s in option["stops"]], ["S2", "S3", "S4"])
        self.assertEqual(option["stop_count"], 2)
        self.assertAlmostEqual(option["ride_distance_km"], 2.0, delta=0.01)
        self.assertEqual(option["scheduled_ride_minutes"], 10)
        live = option["live_buses"]
        self.assertEqual([b["vehicle_id"] for b in live], ["V_EAST"])  # V_OFF off route, V_OLD stale
        self.assertEqual(live[0]["stops_away"], 0)
        self.assertAlmostEqual(live[0]["distance_to_board_km"], 0.5, delta=0.01)
        self.assertEqual(live[0]["eta_source"], "realtime")
        self.assertEqual(live[0]["bus_type"], "City Ordinary")
        # Eastbound pattern carries both trip types; route bus_type = most common on this pattern
        self.assertEqual(set(option["bus_types"]), {"City Ordinary", "Metro Express"})
        self.assertIsNotNone(live[0]["eta_at_alight"])
        self.assertGreaterEqual(len(option["path"]), 2)
        self.assertIsInstance(option["next_scheduled"], list)

    def test_reverse_journey_uses_westbound_and_skips_bus_that_already_passed(self):
        body = self.client.get("/api/journeys/", {"from_stop": "S4", "to_stop": "S2"}).json()
        self.assertEqual(body["count"], 1)
        option = body["options"][0]
        self.assertEqual((option["towards"], option["headsign"]), ("Stop 1", "Far Town"))
        self.assertEqual(option["live_buses"], [])  # V_WEST is already past S4

    def test_journey_by_coordinates_and_no_stop_nearby(self):
        body = self.client.get("/api/journeys/", {
            "from_lat": STOP_LAT, "from_lon": lon_at(1.1), "to_lat": STOP_LAT, "to_lon": lon_at(2.9),
        }).json()
        self.assertEqual(body["options"][0]["board"]["stop_id"], "S2")
        self.assertGreater(body["options"][0]["board"]["walk_m"], 50)
        far = self.client.get("/api/journeys/", {"from_lat": 18.5, "from_lon": 84.0, "to_stop": "S4"}).json()
        self.assertEqual(far["count"], 0)
        self.assertIn("walking distance", far["message"])

    def test_journey_validation(self):
        self.assertEqual(self.client.get("/api/journeys/", {"from_stop": "S1"}).status_code, 400)
        self.assertEqual(self.client.get("/api/journeys/", {"from_stop": "NOPE", "to_stop": "S1"}).status_code, 404)

    # ---- stops -------------------------------------------------------------
    def test_stop_arrivals(self):
        body = self.client.get("/api/stops/S3/arrivals/").json()
        self.assertEqual([b["vehicle_id"] for b in body["results"]], ["V_EAST"])
        self.assertEqual(body["results"][0]["stops_away"], 1)

    def test_buses_via_stop_relation_status_and_category(self):
        body = self.client.get("/api/stops/buses/", {"q": "Stop 3"}).json()
        self.assertEqual([s["stop_id"] for s in body["stops"]], ["S3"])
        by_id = {b["vehicle_id"]: b for b in body["results"]}
        # Every live bus whose trip serves S3; V_NOGTFS (no trip in GTFS) is not included
        self.assertEqual(set(by_id), {"V_EAST", "V_WEST", "V_OFF", "V_OLD"})
        east = by_id["V_EAST"]["via"]
        self.assertEqual((east["relation"], east["status"], east["stops_away"]), ("passes", "approaching", 1))
        self.assertAlmostEqual(east["distance_km"], 1.5, delta=0.01)
        self.assertIsNotNone(east["eta"])
        self.assertEqual(by_id["V_WEST"]["via"]["status"], "passed")    # westbound at 1.5 km, S3 is at 1 km on its trip
        self.assertEqual(by_id["V_OFF"]["via"]["status"], "unknown")    # off route: never guessed
        self.assertEqual(by_id["V_OLD"]["via"]["status"], "unknown")    # stale GPS
        self.assertEqual(body["results"][0]["vehicle_id"], "V_EAST")    # approaching first
        # Category from each bus's own trip_type
        self.assertEqual(by_id["V_OFF"]["bus_category"], "metro")       # Metro Express
        self.assertEqual(by_id["V_EAST"]["bus_category"], "local")      # City Ordinary
        self.assertEqual(body["counts"], {"all": 4, "metro": 1, "local": 3, "other": 0})

        metro = self.client.get("/api/stops/buses/", {"q": "Stop 3", "category": "metro"}).json()
        self.assertEqual([b["vehicle_id"] for b in metro["results"]], ["V_OFF"])
        self.assertEqual(metro["counts"]["all"], 4)  # counts always cover every group

        ends = self.client.get("/api/stops/buses/", {"stop_id": "S1"}).json()
        relations = {b["vehicle_id"]: b["via"]["relation"] for b in ends["results"]}
        self.assertEqual(relations["V_EAST"], "starts")
        self.assertEqual(relations["V_WEST"], "ends")
        self.assertEqual(self.client.get("/api/stops/buses/").status_code, 400)
        self.assertEqual(self.client.get("/api/stops/buses/", {"stop_id": "NOPE"}).status_code, 404)

    def test_nearby_and_search_stops(self):
        body = self.client.get("/api/stops/nearby/", {"latitude": STOP_LAT, "longitude": lon_at(1), "radius": 0.5}).json()
        self.assertEqual([s["stop_id"] for s in body["results"]], ["S2"])
        self.assertEqual([r["route_number"] for r in body["results"][0]["routes"]], ["A009"])
        found = self.client.get("/api/stops/search/", {"q": "stop 3"}).json()["results"]
        self.assertEqual(found[0]["stop_id"], "S3")
        self.assertEqual({r["route_number"] for r in found[0]["routes"]}, {"A009", "X100"})
        self.assertEqual(self.client.get("/api/stops/S1/").json()["result"]["stop_name"], "Stop 1")

    # ---- routes / trips ------------------------------------------------------
    def test_routes_list_search_detail(self):
        listing = self.client.get("/api/routes/").json()
        self.assertEqual(listing["count"], 2)
        a009 = next(r for r in listing["results"] if r["route_number"] == "A009")
        self.assertEqual(a009["active_buses"], 4)
        self.assertEqual(self.client.get("/api/routes/", {"service_type": "intercity"}).json()["count"], 1)
        self.assertEqual(self.client.get("/api/routes/search/", {"q": "A009"}).json()["count"], 1)
        self.assertEqual(self.client.get("/api/routes/search/", {"q": "Stop 2"}).json()["count"], 1)  # via stop name

        detail = self.client.get(f"/api/routes/{ROUTE}/").json()["result"]
        self.assertEqual(len(detail["patterns"]), 2)
        self.assertEqual(detail["stops"][0]["stop_id"], "S1")  # busiest pattern first (eastbound, 2 trips)
        self.assertFalse(detail["path_approximate"])
        west = next(p for p in detail["patterns"] if p["towards"] == "Stop 1")
        stops = self.client.get(f"/api/routes/{ROUTE}/stops/", {"pattern_id": west["pattern_id"]}).json()
        self.assertEqual([s["stop_id"] for s in stops["results"]], ["S4", "S3", "S2", "S1"])
        self.assertTrue(self.client.get("/api/routes/x100-stop-1-stop-3/").json()["result"]["path_approximate"])
        self.assertEqual(self.client.get("/api/routes/nope/").status_code, 404)

    def test_trip_detail_service_day_times(self):
        body = self.client.get("/api/trips/T_EAST2/").json()["result"]
        self.assertEqual([s["scheduled_time"] for s in body["stops"]], ["09:00", "09:05", "09:10", "09:15"])

    def test_feed_status_reports_gtfs(self):
        gtfs = self.client.get("/api/feed/status/").json()["gtfs"]
        self.assertEqual((gtfs["routes"], gtfs["trips"], gtfs["version"]), (2, 4, "test-1"))
        self.assertEqual(gtfs["buses_with_progress"], 3)  # V_EAST, V_WEST, V_OFF (off_route)


class DownloadTests(FeedTestCase):
    def test_download_extracts_only_gtfs_files(self):
        import io
        import tempfile
        import zipfile
        from pathlib import Path
        from unittest import mock

        from apps.transit.services.gtfs_import import download_gtfs

        buffer = io.BytesIO()
        with GtfsFolder() as folder, zipfile.ZipFile(buffer, "w") as archive:
            for txt in folder.glob("*.txt"):
                archive.write(txt, f"apsrtc/{txt.name}")  # nested folder inside the zip
            archive.writestr("../../evil.txt", "nope")    # path traversal attempt
            archive.writestr("notes.md", "ignored")

        class FakeStream:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def iter_content(self, chunk_size):
                yield buffer.getvalue()

        session = mock.Mock()
        session.get.return_value = FakeStream()
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "apps.buses.services.apsrtc_service.build_session", return_value=session
        ):
            out = download_gtfs("https://example.invalid/gtfs.zip", Path(tmp) / "feed")
            names = sorted(p.name for p in out.iterdir())
            self.assertIn("stops.txt", names)
            self.assertNotIn("evil.txt", names)
            self.assertNotIn("notes.md", names)
            self.assertFalse((Path(tmp) / "evil.txt").exists())
            self.assertEqual(import_gtfs(out).trips, 4)


class NotImportedTests(FeedTestCase):
    def test_gtfs_endpoints_answer_not_available(self):
        for url in ("/api/journeys/?from_stop=a&to_stop=b", "/api/stops/search/?q=ab", "/api/trips/x/"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 501)
                self.assertEqual(response.json()["error"]["code"], "not_available")
