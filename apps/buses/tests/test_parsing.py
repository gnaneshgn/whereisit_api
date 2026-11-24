import time
from datetime import date

from django.test import SimpleTestCase

from apps.buses.services import apsrtc_service
from apps.buses.services.apsrtc_service import FeedInvalidError
from common.utilities import epoch_to_datetime, utc_now

from .factories import feed, status_payload, trip_updates_feed, vehicle_entity


class VehiclePositionParsingTests(SimpleTestCase):
    def setUp(self):
        self.now_ts = int(time.time())
        self.now = utc_now()

    def parse(self, entities):
        return apsrtc_service.parse_vehicle_positions(feed(entities, self.now_ts), self.now)

    def test_parses_real_feed_shape(self):
        snapshot = self.parse([vehicle_entity(ts=self.now_ts - 20)])
        self.assertEqual(snapshot.feed_timestamp, epoch_to_datetime(self.now_ts))
        self.assertEqual(len(snapshot.vehicles), 1)
        v = snapshot.vehicles[0]
        self.assertEqual(v.vehicle_id, "24112026_A033_11_WALTAIR")
        self.assertEqual(v.registration_number, "AP27Z0208")
        self.assertEqual(v.trip_id, "A033_11_WALTAIR")
        self.assertEqual(v.route_id, "a033-bheemunipatnam-railway-station-vsp")
        self.assertEqual(v.trip_start_date, date(2026, 11, 24))
        self.assertEqual((v.latitude, v.longitude, v.bearing, v.speed), (17.75187, 83.3499, 210.4, 7.5))
        self.assertEqual(v.gps_timestamp, epoch_to_datetime(self.now_ts - 20))
        self.assertIsNone(v.raw)

    def test_numeric_and_string_timestamps_both_accepted(self):
        entity = vehicle_entity()
        entity["vehicle"]["timestamp"] = self.now_ts - 5  # int instead of string
        v = self.parse([entity]).vehicles[0]
        self.assertEqual(v.gps_timestamp, epoch_to_datetime(self.now_ts - 5))

    def test_missing_vehicle_id_falls_back_to_entity_id(self):
        entity = vehicle_entity(vehicle_id="ENTITY-1", ts=self.now_ts)
        del entity["vehicle"]["vehicle"]["id"]
        snapshot = self.parse([entity])
        self.assertEqual(snapshot.vehicles[0].vehicle_id, "ENTITY-1")

    def test_missing_all_ids_skips_record(self):
        entity = vehicle_entity(ts=self.now_ts)
        del entity["vehicle"]["vehicle"]["id"]
        del entity["id"]
        snapshot = self.parse([entity, vehicle_entity(vehicle_id="OK", ts=self.now_ts)])
        self.assertEqual([v.vehicle_id for v in snapshot.vehicles], ["OK"])
        self.assertEqual(snapshot.stats.skipped_no_id, 1)

    def test_entity_without_vehicle_block_is_skipped(self):
        snapshot = self.parse([{"id": "x", "tripUpdate": {}}, "garbage", None])
        self.assertEqual(snapshot.vehicles, [])
        self.assertEqual(snapshot.stats.skipped_no_vehicle, 3)

    def test_missing_gps_coordinates_kept_without_position(self):
        entity = vehicle_entity(ts=self.now_ts)
        del entity["vehicle"]["position"]
        zero = vehicle_entity(vehicle_id="ZERO", lat=0, lon=0, ts=self.now_ts)
        bad = vehicle_entity(vehicle_id="BAD", lat=123.0, lon="abc", ts=self.now_ts)
        snapshot = self.parse([entity, zero, bad])
        self.assertEqual(len(snapshot.vehicles), 3)
        for v in snapshot.vehicles:
            self.assertIsNone(v.latitude)
            self.assertIsNone(v.longitude)
        self.assertEqual(snapshot.stats.missing_position, 3)

    def test_missing_trip_and_route_are_none_not_invented(self):
        snapshot = self.parse([vehicle_entity(trip_id=None, route_id=None, ts=self.now_ts)])
        v = snapshot.vehicles[0]
        self.assertIsNone(v.trip_id)
        self.assertIsNone(v.route_id)
        self.assertIsNone(v.trip_start_date)
        self.assertEqual(snapshot.stats.missing_trip, 1)

    def test_missing_route_id_only(self):
        v = self.parse([vehicle_entity(route_id=None, ts=self.now_ts)]).vehicles[0]
        self.assertEqual(v.trip_id, "A033_11_WALTAIR")
        self.assertIsNone(v.route_id)

    def test_registration_from_label_when_plate_missing_and_empty_plate_is_none(self):
        a = vehicle_entity(vehicle_id="A", plate="ap 31 z-0475", ts=self.now_ts)
        del a["vehicle"]["vehicle"]["licensePlate"]
        b = vehicle_entity(vehicle_id="B", plate="", ts=self.now_ts)
        c = vehicle_entity(vehicle_id="C", plate="39Z0142", ts=self.now_ts)  # partial plates are kept as-is
        regs = {v.vehicle_id: v.registration_number for v in self.parse([a, b, c]).vehicles}
        self.assertEqual(regs, {"A": "AP31Z0475", "B": None, "C": "39Z0142"})

    def test_future_timestamp_is_discarded(self):
        v = self.parse([vehicle_entity(ts=self.now_ts + 86400 * 365)]).vehicles[0]
        self.assertIsNone(v.gps_timestamp)

    def test_missing_timestamp(self):
        v = self.parse([vehicle_entity(ts=None)]).vehicles[0]
        self.assertIsNone(v.gps_timestamp)

    def test_invalid_bearing_and_negative_speed_dropped(self):
        v = self.parse([vehicle_entity(bearing=999, speed=-3, ts=self.now_ts)]).vehicles[0]
        self.assertIsNone(v.bearing)
        self.assertIsNone(v.speed)

    def test_duplicate_vehicle_ids_keep_newest(self):
        old = vehicle_entity(vehicle_id="DUP", lat=17.1, ts=self.now_ts - 300)
        new = vehicle_entity(vehicle_id="DUP", lat=17.2, ts=self.now_ts - 10)
        snapshot = self.parse([new, old])
        self.assertEqual(len(snapshot.vehicles), 1)
        self.assertEqual(snapshot.vehicles[0].latitude, 17.2)
        self.assertEqual(snapshot.stats.duplicate_ids, 1)

    def test_invalid_structure_raises(self):
        for payload in (None, [], "text", {"header": {}}, {"entity": {}}):
            with self.subTest(payload=payload), self.assertRaises(FeedInvalidError):
                apsrtc_service.parse_vehicle_positions(payload, self.now)

    def test_empty_entity_list_parses_to_empty_snapshot(self):
        self.assertEqual(self.parse([]).vehicles, [])


class OtherFeedParsingTests(SimpleTestCase):
    def test_trip_updates_keyed_by_vehicle_with_next_stop(self):
        ts = int(time.time())
        feed_ts, updates = apsrtc_service.parse_trip_updates(trip_updates_feed(ts))
        self.assertEqual(feed_ts, epoch_to_datetime(ts))
        update = updates["24112026_A033_11_WALTAIR"]
        self.assertEqual(update["trip_id"], "A033_11_WALTAIR")
        self.assertEqual(update["next_stop"]["stop_id"], "237961")
        self.assertEqual(update["next_stop"]["stop_sequence"], 13)
        self.assertEqual(update["next_stop"]["delay_seconds"], 60)
        self.assertEqual(update["remaining_stop_updates"], 2)

    def test_status_parsing(self):
        parsed = apsrtc_service.parse_status(status_payload(1795527505, vehicles=6877))
        self.assertEqual(parsed["network"], "apsrtc")
        self.assertEqual(parsed["vehicles"], 6877)
        self.assertTrue(parsed["feed_time"].startswith("2026-11-24T"))

    def test_status_must_be_object(self):
        with self.assertRaises(FeedInvalidError):
            apsrtc_service.parse_status(["nope"])
