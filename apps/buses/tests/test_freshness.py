from datetime import timedelta

from django.test import SimpleTestCase, override_settings

from apps.buses.models import GpsStatus
from apps.buses.services.feed_status_service import classify_gps_status
from common.utilities import utc_now

THRESHOLDS = {"LIVE_MAX_AGE": 90, "RECENT_MAX_AGE": 300, "DELAYED_MAX_AGE": 1800, "FUTURE_TOLERANCE": 120}


@override_settings(GPS_FRESHNESS=THRESHOLDS)
class GpsFreshnessTests(SimpleTestCase):
    def setUp(self):
        self.now = utc_now()

    def status_for(self, age_seconds):
        return classify_gps_status(self.now - timedelta(seconds=age_seconds), self.now)

    def test_boundaries(self):
        cases = [
            (0, GpsStatus.LIVE), (90, GpsStatus.LIVE), (91, GpsStatus.RECENT),
            (300, GpsStatus.RECENT), (301, GpsStatus.DELAYED), (1800, GpsStatus.DELAYED),
            (1801, GpsStatus.UNAVAILABLE), (86400, GpsStatus.UNAVAILABLE),
        ]
        for age, expected in cases:
            with self.subTest(age=age):
                self.assertEqual(self.status_for(age), expected)

    def test_missing_timestamp_is_unavailable(self):
        self.assertEqual(classify_gps_status(None, self.now), GpsStatus.UNAVAILABLE)

    def test_small_clock_skew_is_live_but_far_future_is_unavailable(self):
        self.assertEqual(self.status_for(-60), GpsStatus.LIVE)
        self.assertEqual(self.status_for(-3600), GpsStatus.UNAVAILABLE)

    @override_settings(GPS_FRESHNESS={**THRESHOLDS, "LIVE_MAX_AGE": 30})
    def test_thresholds_are_configurable(self):
        self.assertEqual(self.status_for(45), GpsStatus.RECENT)
