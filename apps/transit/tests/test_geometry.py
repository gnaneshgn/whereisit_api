from django.test import SimpleTestCase

from apps.transit.services.geometry import (
    Polyline,
    bearing_difference,
    decode_polyline,
    encode_polyline,
    haversine_m,
    locate_stops,
)
from apps.transit.tests.fixtures import LAT, lon_at


class PolylineCodecTests(SimpleTestCase):
    def test_round_trip_keeps_five_decimals(self):
        points = [(17.72612, 83.30155), (17.7, 83.2), (-12.34567, 120.98765)]
        self.assertEqual(decode_polyline(encode_polyline(points)), points)

    def test_known_google_example(self):
        # Example from Google's polyline algorithm documentation
        self.assertEqual(encode_polyline([(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]),
                         "_p~iF~ps|U_ulLnnqC_mqNvxq`@")


class ProjectionTests(SimpleTestCase):
    def setUp(self):
        self.line = Polyline([(LAT, lon_at(0)), (LAT, lon_at(1.5)), (LAT, lon_at(3))])

    def test_length_and_projection_on_sparse_segments(self):
        self.assertAlmostEqual(self.line.length_m, 3000, delta=3)
        # 1.2 km along, 40 m north: must snap onto the segment, not the 1.5 km vertex
        (c,) = self.line.candidates(LAT + 40 / 111195.0, lon_at(1.2), 150)
        self.assertAlmostEqual(c.along_m, 1200, delta=3)
        self.assertAlmostEqual(c.offset_m, 40, delta=1)
        self.assertAlmostEqual(c.bearing, 90, delta=0.5)

    def test_far_point_has_no_candidates(self):
        self.assertEqual(self.line.candidates(LAT + 0.01, lon_at(1), 150), [])

    def test_out_and_back_gives_two_candidates(self):
        there_and_back = Polyline([(LAT, lon_at(0)), (LAT, lon_at(2)), (LAT + 0.0003, lon_at(2)), (LAT + 0.0003, lon_at(0))])
        cands = there_and_back.candidates(LAT + 0.00015, lon_at(1), 150)
        self.assertEqual(len(cands), 2)
        self.assertEqual({round(c.bearing) for c in cands}, {90, 270})

    def test_locate_stops_is_monotonic_on_a_loop(self):
        # Out-and-back on the same road (turnaround stop at 2 km): the return stops
        # must land on the return leg even though the outbound leg is just as close.
        line = Polyline([(LAT, lon_at(0)), (LAT, lon_at(2)), (LAT, lon_at(0))])
        stops = [(LAT, lon_at(km)) for km in (0.5, 1.5, 2.0, 1.5, 0.5)]
        alongs = [round(a / 100) * 100 for a, _ in locate_stops(line, stops)]
        self.assertEqual(alongs, [500, 1500, 2000, 2500, 3500])

    def test_slice_and_point_at(self):
        part = self.line.slice(500, 2000)
        self.assertAlmostEqual(haversine_m(*part[0], LAT, lon_at(0.5)), 0, delta=1)
        self.assertAlmostEqual(haversine_m(*part[-1], LAT, lon_at(2)), 0, delta=1)
        self.assertEqual(len(part), 3)  # start, the 1.5 km vertex, end

    def test_bearing_difference_wraps(self):
        self.assertEqual(bearing_difference(350, 10), 20)
        self.assertEqual(bearing_difference(90, 270), 180)
