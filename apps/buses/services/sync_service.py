"""
Orchestrates feed synchronization: fetch -> validate -> match -> persist -> log.

Guarantees:
  * Never overwrite good data with an empty, invalid or older snapshot.
  * Concurrent runs are prevented with a cache lock (Redis SET NX in prod).
  * Every attempt is recorded in FeedSyncLog and the cached feed state.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from apps.buses.models import FeedSource, LiveBus, SyncStatus
from apps.buses.services import apsrtc_service, bus_matching_service, feed_status_service
from apps.buses.services.apsrtc_service import FeedError, FeedInvalidError
from common.utilities import utc_now

logger = logging.getLogger(__name__)

KEY_LAST_GOOD_SNAPSHOT = "feed:vehicle_positions:last_good"
KEY_LOCK = "lock:sync:{source}"
KEY_DATA_VERSION = "feed:vehicle_positions:version"

UPSERT_FIELDS = [
    "registration_number", "trip_id", "route_id", "trip_start_date", "route_number", "destination",
    "latitude", "longitude", "bearing", "speed", "gps_timestamp", "gps_status", "is_superseded",
    "last_synced_at", "raw_data",
]


@dataclass
class SyncOutcome:
    status: str
    total_vehicles: int | None = None
    message: str = ""


@contextmanager
def sync_lock(source: str, ttl: int):
    """Non-blocking lock; yields False if another run holds it."""
    key = KEY_LOCK.format(source=source)
    token = uuid.uuid4().hex
    acquired = cache.add(key, token, timeout=ttl)
    try:
        yield acquired
    finally:
        if acquired and cache.get(key) == token:
            cache.delete(key)


def get_data_version() -> int:
    return cache.get(KEY_DATA_VERSION) or 0


def _bump_data_version() -> None:
    try:
        cache.incr(KEY_DATA_VERSION)
    except ValueError:
        cache.set(KEY_DATA_VERSION, 1, timeout=None)


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _lock_ttl() -> int:
    cfg = settings.APSRTC
    # Worst case one run takes (retries + 1) * (connect + read) + backoff; cap the lock just above that.
    return (cfg["MAX_RETRIES"] + 1) * (cfg["CONNECT_TIMEOUT"] + cfg["READ_TIMEOUT"]) + 30


def persist_snapshot(snapshot, now) -> int:
    """Upsert current vehicles and drop those no longer in the full dataset."""
    vehicles = snapshot.vehicles
    superseded = bus_matching_service.find_superseded_ids(vehicles)
    route_names, headsigns = bus_matching_service.lookup_route_metadata(
        {v.route_id for v in vehicles if v.route_id}, {v.trip_id for v in vehicles if v.trip_id}
    )

    rows = [
        LiveBus(
            vehicle_id=v.vehicle_id,
            registration_number=v.registration_number,
            trip_id=v.trip_id,
            route_id=v.route_id,
            trip_start_date=v.trip_start_date,
            route_number=route_names.get(v.route_id),
            destination=headsigns.get(v.trip_id),
            latitude=v.latitude,
            longitude=v.longitude,
            bearing=v.bearing,
            speed=v.speed,
            gps_timestamp=v.gps_timestamp,
            gps_status=feed_status_service.classify_gps_status(v.gps_timestamp, now),
            is_superseded=v.vehicle_id in superseded,
            last_synced_at=now,
            raw_data=v.raw,
        )
        for v in vehicles
    ]

    with transaction.atomic():
        LiveBus.objects.bulk_create(
            rows,
            batch_size=500,
            update_conflicts=True,
            unique_fields=["vehicle_id"],
            update_fields=UPSERT_FIELDS,
        )
        # Upstream is FULL_DATASET: anything not touched in this run has left the feed.
        removed, _ = LiveBus.objects.filter(last_synced_at__lt=now).delete()
    if removed:
        logger.info("removed %s vehicles no longer present upstream", removed)
    return len(rows)


def sync_vehicle_positions() -> SyncOutcome:
    source = FeedSource.VEHICLE_POSITIONS
    with sync_lock(source, _lock_ttl()) as acquired:
        if not acquired:
            feed_status_service.record_sync(source, SyncStatus.SKIPPED, error_message="previous run still active")
            return SyncOutcome(SyncStatus.SKIPPED, message="previous run still active")
        return _sync_vehicle_positions_locked(source)


def _sync_vehicle_positions_locked(source) -> SyncOutcome:
    start = time.monotonic()
    now = utc_now()
    try:
        result, snapshot = apsrtc_service.fetch_vehicle_positions(now, etag=feed_status_service.get_etag(source))
    except FeedError as exc:
        status = SyncStatus.REJECTED if isinstance(exc, FeedInvalidError) else SyncStatus.FAILED
        feed_status_service.record_sync(source, status, error_message=str(exc), duration_ms=_elapsed_ms(start), now=now)
        return SyncOutcome(status, message=str(exc))

    if result.not_modified:
        feed_status_service.record_sync(
            source, SyncStatus.NOT_MODIFIED, feed_timestamp=feed_status_service.applied_feed_timestamp(source),
            duration_ms=_elapsed_ms(start), now=now,
        )
        return SyncOutcome(SyncStatus.NOT_MODIFIED)

    stats = snapshot.stats
    if not snapshot.vehicles:
        message = f"snapshot contained no usable vehicles ({stats.total_entities} entities)"
        feed_status_service.record_sync(
            source, SyncStatus.REJECTED, total_vehicles=0, skipped_records=stats.skipped,
            feed_timestamp=snapshot.feed_timestamp, error_message=message, duration_ms=_elapsed_ms(start), now=now,
        )
        return SyncOutcome(SyncStatus.REJECTED, 0, message)

    applied = feed_status_service.applied_feed_timestamp(source)
    has_rows = LiveBus.objects.exists()
    if applied and snapshot.feed_timestamp and has_rows:
        if snapshot.feed_timestamp < applied:
            message = f"snapshot {snapshot.feed_timestamp.isoformat()} is older than stored {applied.isoformat()}"
            feed_status_service.record_sync(
                source, SyncStatus.REJECTED, total_vehicles=len(snapshot.vehicles),
                feed_timestamp=snapshot.feed_timestamp, error_message=message, duration_ms=_elapsed_ms(start), now=now,
            )
            return SyncOutcome(SyncStatus.REJECTED, len(snapshot.vehicles), message)
        if snapshot.feed_timestamp == applied:
            feed_status_service.set_etag(source, result.etag)
            feed_status_service.record_sync(
                source, SyncStatus.NOT_MODIFIED, total_vehicles=len(snapshot.vehicles),
                feed_timestamp=applied, duration_ms=_elapsed_ms(start), now=now,
            )
            return SyncOutcome(SyncStatus.NOT_MODIFIED, len(snapshot.vehicles))

    count = persist_snapshot(snapshot, now)
    cache.set(
        KEY_LAST_GOOD_SNAPSHOT,
        {
            "feed_timestamp": snapshot.feed_timestamp.isoformat() if snapshot.feed_timestamp else None,
            "synced_at": now.isoformat(),
            "vehicles": [v.to_cache_dict() for v in snapshot.vehicles],
        },
        timeout=None,
    )
    feed_status_service.set_etag(source, result.etag)
    _bump_data_version()

    notes = (
        f"no_trip={stats.missing_trip} no_position={stats.missing_position} "
        f"no_registration={stats.missing_registration} invalid_ts={stats.invalid_timestamp} "
        f"duplicate_ids={stats.duplicate_ids}"
    )
    feed_status_service.record_sync(
        source, SyncStatus.SUCCESS, total_vehicles=count, skipped_records=stats.skipped,
        feed_timestamp=snapshot.feed_timestamp, error_message=notes, duration_ms=_elapsed_ms(start), now=now,
    )
    return SyncOutcome(SyncStatus.SUCCESS, count, notes)


def sync_trip_updates() -> SyncOutcome:
    source = FeedSource.TRIP_UPDATES
    with sync_lock(source, _lock_ttl()) as acquired:
        if not acquired:
            feed_status_service.record_sync(source, SyncStatus.SKIPPED, error_message="previous run still active")
            return SyncOutcome(SyncStatus.SKIPPED)

        start = time.monotonic()
        now = utc_now()
        try:
            result, feed_timestamp, updates = apsrtc_service.fetch_trip_updates(
                etag=feed_status_service.get_etag(source)
            )
        except FeedError as exc:
            status = SyncStatus.REJECTED if isinstance(exc, FeedInvalidError) else SyncStatus.FAILED
            feed_status_service.record_sync(source, status, error_message=str(exc), duration_ms=_elapsed_ms(start), now=now)
            return SyncOutcome(status, message=str(exc))

        if result.not_modified:
            feed_status_service.record_sync(source, SyncStatus.NOT_MODIFIED, duration_ms=_elapsed_ms(start), now=now)
            return SyncOutcome(SyncStatus.NOT_MODIFIED)

        if not updates:
            feed_status_service.record_sync(
                source, SyncStatus.REJECTED, total_vehicles=0, feed_timestamp=feed_timestamp,
                error_message="trip updates snapshot was empty", duration_ms=_elapsed_ms(start), now=now,
            )
            return SyncOutcome(SyncStatus.REJECTED, 0, "empty snapshot")

        bus_matching_service.store_trip_updates(updates)
        feed_status_service.set_etag(source, result.etag)
        feed_status_service.record_sync(
            source, SyncStatus.SUCCESS, total_vehicles=len(updates), feed_timestamp=feed_timestamp,
            duration_ms=_elapsed_ms(start), now=now,
        )
        return SyncOutcome(SyncStatus.SUCCESS, len(updates))


def check_feed_status() -> SyncOutcome:
    source = FeedSource.STATUS
    start = time.monotonic()
    now = utc_now()
    try:
        status = apsrtc_service.fetch_status()
    except FeedError as exc:
        result_status = SyncStatus.REJECTED if isinstance(exc, FeedInvalidError) else SyncStatus.FAILED
        feed_status_service.record_sync(source, result_status, error_message=str(exc), duration_ms=_elapsed_ms(start), now=now)
        return SyncOutcome(result_status, message=str(exc))

    feed_status_service.store_upstream_status(status, now)
    feed_time = datetime.fromisoformat(status["feed_time"]) if status.get("feed_time") else None
    feed_status_service.record_sync(
        source, SyncStatus.SUCCESS, total_vehicles=status.get("vehicles"), feed_timestamp=feed_time,
        duration_ms=_elapsed_ms(start), now=now,
    )
    pruned = feed_status_service.prune_sync_logs(now)
    if pruned:
        logger.info("pruned %s old FeedSyncLog rows", pruned)
    return SyncOutcome(SyncStatus.SUCCESS, status.get("vehicles"))
