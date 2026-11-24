import time
from datetime import timedelta
from unittest import mock

import requests
from django.core.cache import cache

from apps.buses.models import FeedSource, FeedSyncLog, GpsStatus, LiveBus, SyncStatus
from apps.buses.services import feed_status_service, sync_service
from apps.buses.services.feed_status_service import FeedHealth
from common.utilities import utc_now

from .factories import FakeResponse, FeedTestCase, feed, patch_http, status_payload, trip_updates_feed, vehicle_entity


class VehicleSyncTests(FeedTestCase):
    def setUp(self):
        super().setUp()
        self.ts = int(time.time())

    def run_sync(self, *responses):
        patcher, session = patch_http({"vehicle_positions": list(responses)})
        with patcher:
            outcome = sync_service.sync_vehicle_positions()
        return outcome, session

    def ok(self, entities, header_ts=None, etag=None):
        return FakeResponse(200, feed(entities, header_ts or self.ts), headers={"ETag": etag} if etag else {})

    def seed(self):
        outcome, _ = self.run_sync(self.ok([
            vehicle_entity(vehicle_id="V1", plate="AP31Z0001", ts=self.ts - 10),
            vehicle_entity(vehicle_id="V2", plate="AP31Z0002", ts=self.ts - 400),
        ], header_ts=self.ts - 30))
        self.assertEqual(outcome.status, SyncStatus.SUCCESS)

    def test_successful_sync_persists_and_logs(self):
        self.seed()
        self.assertEqual(LiveBus.objects.count(), 2)
        v1 = LiveBus.objects.get(vehicle_id="V1")
        self.assertEqual(v1.registration_number, "AP31Z0001")
        self.assertEqual(v1.gps_status, GpsStatus.LIVE)
        self.assertIsNone(v1.route_number)  # no GTFS yet: never guessed
        self.assertEqual(LiveBus.objects.get(vehicle_id="V2").gps_status, GpsStatus.DELAYED)
        log = FeedSyncLog.objects.get(source=FeedSource.VEHICLE_POSITIONS)
        self.assertEqual((log.status, log.total_vehicles), (SyncStatus.SUCCESS, 2))
        self.assertIsNotNone(log.successful_sync_time)
        self.assertEqual(len(cache.get(sync_service.KEY_LAST_GOOD_SNAPSHOT)["vehicles"]), 2)

    def test_next_snapshot_updates_and_removes_vanished_vehicles(self):
        self.seed()
        self.run_sync(self.ok([vehicle_entity(vehicle_id="V1", plate="AP31Z0001", lat=17.9, ts=self.ts)]))
        self.assertEqual(list(LiveBus.objects.values_list("vehicle_id", flat=True)), ["V1"])
        self.assertEqual(LiveBus.objects.get().latitude, 17.9)

    def test_feed_unavailable_keeps_last_good_data(self):
        self.seed()
        outcome, _ = self.run_sync(requests.ConnectionError("boom"))
        self.assertEqual(outcome.status, SyncStatus.FAILED)
        self.assertEqual(LiveBus.objects.count(), 2)
        state = feed_status_service.get_source_state(FeedSource.VEHICLE_POSITIONS)
        self.assertEqual(state["consecutive_failures"], 1)
        self.assertIn("boom", state["last_error"])

    def test_http_error_status_keeps_data(self):
        self.seed()
        outcome, _ = self.run_sync(FakeResponse(503, None))
        self.assertEqual(outcome.status, SyncStatus.FAILED)
        self.assertEqual(LiveBus.objects.count(), 2)

    def test_invalid_json_is_rejected_and_keeps_data(self):
        self.seed()
        outcome, _ = self.run_sync(FakeResponse(200, text="<html>bad gateway</html>"))
        self.assertEqual(outcome.status, SyncStatus.REJECTED)
        self.assertEqual(LiveBus.objects.count(), 2)

    def test_empty_snapshot_is_rejected(self):
        self.seed()
        outcome, _ = self.run_sync(self.ok([], header_ts=self.ts))
        self.assertEqual(outcome.status, SyncStatus.REJECTED)
        self.assertEqual(LiveBus.objects.count(), 2)

    def test_older_snapshot_is_rejected(self):
        self.seed()
        outcome, _ = self.run_sync(self.ok([vehicle_entity(vehicle_id="OLD", ts=self.ts - 900)], header_ts=self.ts - 900))
        self.assertEqual(outcome.status, SyncStatus.REJECTED)
        self.assertFalse(LiveBus.objects.filter(vehicle_id="OLD").exists())

    def test_same_feed_timestamp_is_not_rewritten(self):
        self.seed()
        outcome, _ = self.run_sync(self.ok([vehicle_entity(vehicle_id="NEW", ts=self.ts)], header_ts=self.ts - 30))
        self.assertEqual(outcome.status, SyncStatus.NOT_MODIFIED)
        self.assertFalse(LiveBus.objects.filter(vehicle_id="NEW").exists())

    def test_etag_sent_and_304_handled(self):
        first, _ = self.run_sync(self.ok([vehicle_entity(ts=self.ts)], etag='W/"abc"'))
        self.assertEqual(first.status, SyncStatus.SUCCESS)
        outcome, session = self.run_sync(FakeResponse(304, None))
        self.assertEqual(outcome.status, SyncStatus.NOT_MODIFIED)
        self.assertEqual(session.calls[0][1].get("If-None-Match"), 'W/"abc"')
        self.assertEqual(LiveBus.objects.count(), 1)

    def test_concurrent_run_is_skipped(self):
        cache.add(sync_service.KEY_LOCK.format(source=FeedSource.VEHICLE_POSITIONS), "other", 60)
        outcome, session = self.run_sync(self.ok([vehicle_entity(ts=self.ts)]))
        self.assertEqual(outcome.status, SyncStatus.SKIPPED)
        self.assertEqual(session.calls, [])

    def test_duplicate_registration_marks_older_record_superseded(self):
        self.run_sync(self.ok([
            vehicle_entity(vehicle_id="21112026_OLD", plate="AP37Z0210", ts=self.ts - 600),
            vehicle_entity(vehicle_id="24112026_NEW", plate="AP37Z0210", ts=self.ts - 20),
            vehicle_entity(vehicle_id="NOPLATE1", plate="", ts=self.ts),
            vehicle_entity(vehicle_id="NOPLATE2", plate="", ts=self.ts),
        ]))
        superseded = set(LiveBus.objects.filter(is_superseded=True).values_list("vehicle_id", flat=True))
        self.assertEqual(superseded, {"21112026_OLD"})  # buses without plates are never merged


class TripAndStatusSyncTests(FeedTestCase):
    def test_trip_updates_cached(self):
        ts = int(time.time())
        patcher, _ = patch_http({"trip_updates": FakeResponse(200, trip_updates_feed(ts))})
        with patcher:
            outcome = sync_service.sync_trip_updates()
        self.assertEqual((outcome.status, outcome.total_vehicles), (SyncStatus.SUCCESS, 1))
        from apps.buses.services.bus_matching_service import get_trip_update
        self.assertEqual(get_trip_update("24112026_A033_11_WALTAIR", "A033_11_WALTAIR")["next_stop"]["stop_id"], "237961")
        self.assertIsNone(get_trip_update("24112026_A033_11_WALTAIR", "DIFFERENT_TRIP"))

    def test_status_check_stores_upstream_status_and_prunes_logs(self):
        old = FeedSyncLog.objects.create(source=FeedSource.STATUS, status=SyncStatus.SUCCESS)
        FeedSyncLog.objects.filter(pk=old.pk).update(created_at=utc_now() - timedelta(days=30))
        patcher, _ = patch_http({"status.json": FakeResponse(200, status_payload(int(time.time()), 6877))})
        with patcher:
            outcome = sync_service.check_feed_status()
        self.assertEqual(outcome.status, SyncStatus.SUCCESS)
        self.assertEqual(feed_status_service.get_upstream_status()["vehicles"], 6877)
        self.assertFalse(FeedSyncLog.objects.filter(pk=old.pk).exists())


class FeedHealthTests(FeedTestCase):
    def record(self, status, seconds_ago, feed_age=None):
        now = utc_now() - timedelta(seconds=seconds_ago)
        feed_ts = now - timedelta(seconds=feed_age) if feed_age is not None else None
        feed_status_service.record_sync(FeedSource.VEHICLE_POSITIONS, status, feed_timestamp=feed_ts, now=now)

    def test_unknown_before_first_sync(self):
        self.assertEqual(feed_status_service.compute_feed_health(), FeedHealth.UNKNOWN)

    def test_healthy(self):
        self.record(SyncStatus.SUCCESS, 5, feed_age=20)
        self.assertEqual(feed_status_service.compute_feed_health(), FeedHealth.HEALTHY)

    def test_degraded_after_failure(self):
        self.record(SyncStatus.SUCCESS, 40, feed_age=20)
        self.record(SyncStatus.FAILED, 5)
        self.assertEqual(feed_status_service.compute_feed_health(), FeedHealth.DEGRADED)

    def test_stale_when_upstream_stops_advancing(self):
        self.record(SyncStatus.SUCCESS, 5, feed_age=900)
        self.assertEqual(feed_status_service.compute_feed_health(), FeedHealth.STALE)

    def test_down_after_long_outage(self):
        self.record(SyncStatus.SUCCESS, 3600, feed_age=10)
        self.assertEqual(feed_status_service.compute_feed_health(), FeedHealth.DOWN)

    def test_last_success_survives_cache_flush(self):
        self.record(SyncStatus.SUCCESS, 5, feed_age=20)
        cache.clear()
        self.assertIsNotNone(feed_status_service.last_successful_sync())


class CeleryTaskTests(FeedTestCase):
    def test_tasks_delegate_to_services(self):
        from apps.buses import tasks
        outcome = sync_service.SyncOutcome(SyncStatus.SUCCESS, 3)
        with mock.patch.object(sync_service, "sync_vehicle_positions", return_value=outcome):
            self.assertEqual(tasks.sync_live_vehicle_positions.apply().get(), {"status": "success", "vehicles": 3})
