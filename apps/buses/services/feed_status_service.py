"""
Feed freshness and synchronization state.

Two different clocks matter and are never conflated:
  * feed / sync time: when upstream generated the snapshot and when we fetched it;
  * vehicle GPS time: when each bus last reported a position.

A vehicle is LIVE only if *its own* GPS timestamp is recent. A successful
backend sync says nothing about an individual bus.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Q

from apps.buses.models import FeedSource, FeedSyncLog, GpsStatus, LiveBus, SyncStatus
from common.utilities import iso_utc, utc_now

logger = logging.getLogger(__name__)

# Cache keys (prefixed globally by settings.CACHES KEY_PREFIX)
KEY_SOURCE_STATE = "feed:state:{source}"  # last attempt / last success per source
KEY_UPSTREAM_STATUS = "feed:upstream_status"  # parsed status.json
KEY_ETAG = "feed:etag:{source}"


class FeedHealth:
    HEALTHY = "healthy"  # recent successful sync and upstream data is fresh
    STALE = "stale"  # sync works, but upstream itself has stopped advancing
    DEGRADED = "degraded"  # latest attempts fail; serving last good data
    DOWN = "down"  # no successful sync for FEED_DOWN_AFTER seconds
    UNKNOWN = "unknown"  # never synced


# ---------------------------------------------------------------------------
# Per-vehicle GPS freshness
# ---------------------------------------------------------------------------
def classify_gps_status(gps_timestamp: datetime | None, now: datetime | None = None) -> str:
    if gps_timestamp is None:
        return GpsStatus.UNAVAILABLE
    now = now or utc_now()
    cfg = settings.GPS_FRESHNESS
    age = (now - gps_timestamp).total_seconds()
    if age < -cfg["FUTURE_TOLERANCE"]:
        return GpsStatus.UNAVAILABLE
    if age <= cfg["LIVE_MAX_AGE"]:
        return GpsStatus.LIVE
    if age <= cfg["RECENT_MAX_AGE"]:
        return GpsStatus.RECENT
    if age <= cfg["DELAYED_MAX_AGE"]:
        return GpsStatus.DELAYED
    return GpsStatus.UNAVAILABLE


def gps_status_q(status: str, now: datetime | None = None) -> Q:
    """
    Q object selecting LiveBus rows whose GPS status *right now* equals ``status``.

    Computed from gps_timestamp at query time so filtering never relies on the
    value stored at the last sync (which ages between syncs).
    """
    now = now or utc_now()
    cfg = settings.GPS_FRESHNESS
    live_after = now - timedelta(seconds=cfg["LIVE_MAX_AGE"])
    recent_after = now - timedelta(seconds=cfg["RECENT_MAX_AGE"])
    delayed_after = now - timedelta(seconds=cfg["DELAYED_MAX_AGE"])
    future_limit = now + timedelta(seconds=cfg["FUTURE_TOLERANCE"])

    if status == GpsStatus.LIVE:
        return Q(gps_timestamp__gte=live_after, gps_timestamp__lte=future_limit)
    if status == GpsStatus.RECENT:
        return Q(gps_timestamp__gte=recent_after, gps_timestamp__lt=live_after)
    if status == GpsStatus.DELAYED:
        return Q(gps_timestamp__gte=delayed_after, gps_timestamp__lt=recent_after)
    if status == GpsStatus.UNAVAILABLE:
        return Q(gps_timestamp__isnull=True) | Q(gps_timestamp__lt=delayed_after) | Q(gps_timestamp__gt=future_limit)
    raise ValueError(f"Unknown GPS status {status!r}")


def available_gps_q(now: datetime | None = None) -> Q:
    """Rows with a usable position whose GPS fix is not UNAVAILABLE."""
    return (
        Q(latitude__isnull=False, longitude__isnull=False)
        & ~gps_status_q(GpsStatus.UNAVAILABLE, now)
    )


# ---------------------------------------------------------------------------
# Sync bookkeeping
# ---------------------------------------------------------------------------
def get_source_state(source: str) -> dict:
    return cache.get(KEY_SOURCE_STATE.format(source=source)) or {}


def get_etag(source: str) -> str | None:
    return cache.get(KEY_ETAG.format(source=source))


def set_etag(source: str, etag: str | None) -> None:
    if etag:
        cache.set(KEY_ETAG.format(source=source), etag, timeout=None)


def record_sync(
    source: str,
    status: str,
    *,
    total_vehicles: int | None = None,
    skipped_records: int | None = None,
    feed_timestamp: datetime | None = None,
    error_message: str = "",
    duration_ms: int | None = None,
    now: datetime | None = None,
) -> FeedSyncLog:
    """Persist a FeedSyncLog row and update the fast cache state for ``source``."""
    now = now or utc_now()
    succeeded = status in (SyncStatus.SUCCESS, SyncStatus.NOT_MODIFIED)
    log = FeedSyncLog.objects.create(
        source=source,
        status=status,
        total_vehicles=total_vehicles,
        skipped_records=skipped_records,
        feed_timestamp=feed_timestamp,
        successful_sync_time=now if succeeded else None,
        error_message=error_message[:2000],
        duration_ms=duration_ms,
    )

    state = get_source_state(source)
    state["last_attempt_at"] = now.isoformat()
    state["last_attempt_status"] = status
    if succeeded:
        state["last_success_at"] = now.isoformat()
        state["consecutive_failures"] = 0
        if feed_timestamp:
            state["feed_timestamp"] = feed_timestamp.isoformat()
        if total_vehicles is not None:
            state["total_vehicles"] = total_vehicles
    elif status in (SyncStatus.FAILED, SyncStatus.REJECTED):
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        state["last_error"] = error_message[:500]
        state["last_error_at"] = now.isoformat()
    cache.set(KEY_SOURCE_STATE.format(source=source), state, timeout=None)

    log_fn = logger.info if succeeded or status == SyncStatus.SKIPPED else logger.warning
    log_fn(
        "sync %s status=%s vehicles=%s skipped=%s feed_ts=%s duration_ms=%s %s",
        source, status, total_vehicles, skipped_records,
        feed_timestamp.isoformat() if feed_timestamp else None, duration_ms, error_message,
    )
    return log


def _parse_iso(value):
    return datetime.fromisoformat(value) if value else None


def last_successful_sync(source: str = FeedSource.VEHICLE_POSITIONS) -> datetime | None:
    """From cache, falling back to the DB (e.g. after a cache flush)."""
    value = _parse_iso(get_source_state(source).get("last_success_at"))
    if value:
        return value
    log = (
        FeedSyncLog.objects.filter(source=source, successful_sync_time__isnull=False)
        .order_by("-successful_sync_time")
        .only("successful_sync_time")
        .first()
    )
    return log.successful_sync_time if log else None


def applied_feed_timestamp(source: str = FeedSource.VEHICLE_POSITIONS) -> datetime | None:
    """Upstream header timestamp of the snapshot currently stored."""
    value = _parse_iso(get_source_state(source).get("feed_timestamp"))
    if value:
        return value
    log = (
        FeedSyncLog.objects.filter(source=source, status=SyncStatus.SUCCESS, feed_timestamp__isnull=False)
        .order_by("-created_at")
        .only("feed_timestamp")
        .first()
    )
    return log.feed_timestamp if log else None


def compute_feed_health(now: datetime | None = None) -> str:
    now = now or utc_now()
    source = FeedSource.VEHICLE_POSITIONS
    last_success = last_successful_sync(source)
    if last_success is None:
        return FeedHealth.UNKNOWN

    cfg = settings.FEED_HEALTH
    if (now - last_success).total_seconds() > cfg["DOWN_AFTER"]:
        return FeedHealth.DOWN

    state = get_source_state(source)
    if state.get("last_attempt_status") in (SyncStatus.FAILED, SyncStatus.REJECTED):
        return FeedHealth.DEGRADED

    feed_ts = applied_feed_timestamp(source)
    if feed_ts is None or (now - feed_ts).total_seconds() > cfg["STALE_AFTER"]:
        return FeedHealth.STALE
    return FeedHealth.HEALTHY


# ---------------------------------------------------------------------------
# Upstream status.json
# ---------------------------------------------------------------------------
def store_upstream_status(status: dict, now: datetime | None = None) -> None:
    now = now or utc_now()
    cache.set(KEY_UPSTREAM_STATUS, {**status, "checked_at": now.isoformat()}, timeout=None)


def get_upstream_status() -> dict | None:
    return cache.get(KEY_UPSTREAM_STATUS)


def prune_sync_logs(now: datetime | None = None) -> int:
    now = now or utc_now()
    cutoff = now - timedelta(days=settings.FEED_HEALTH["SYNC_LOG_RETENTION_DAYS"])
    deleted, _ = FeedSyncLog.objects.filter(created_at__lt=cutoff).delete()
    return deleted


# ---------------------------------------------------------------------------
# API overview
# ---------------------------------------------------------------------------
def gps_status_counts(now: datetime | None = None) -> dict:
    now = now or utc_now()
    aggregates = {
        status.lower(): Count("id", filter=gps_status_q(status, now))
        for status in GpsStatus.values
    }
    counts = LiveBus.objects.filter(is_superseded=False).aggregate(**aggregates)
    return {status: counts[status.lower()] or 0 for status in GpsStatus.values}


def _gtfs_overview() -> dict | None:
    """Imported GTFS static feed and how many live buses have trip progress."""
    from apps.transit.services.trip_context import active_feed  # transit depends on buses

    feed = active_feed()
    if feed is None:
        return None
    return {
        "version": feed.version or None,
        "imported_at": iso_utc(feed.imported_at),
        "service_start": feed.service_start.isoformat() if feed.service_start else None,
        "service_end": feed.service_end.isoformat() if feed.service_end else None,
        "routes": feed.route_count,
        "stops": feed.stop_count,
        "trips": feed.trip_count,
        "patterns": feed.pattern_count,
        "approximate_patterns": feed.approximate_pattern_count,
        "buses_with_route": LiveBus.objects.filter(is_superseded=False).exclude(route_number=None).count(),
        "buses_with_progress": LiveBus.objects.filter(is_superseded=False, progress__isnull=False).count(),
    }


def get_feed_overview(now: datetime | None = None) -> dict:
    now = now or utc_now()
    vp_state = get_source_state(FeedSource.VEHICLE_POSITIONS)
    tu_state = get_source_state(FeedSource.TRIP_UPDATES)
    last_success = last_successful_sync()
    feed_ts = applied_feed_timestamp()
    iso = iso_utc
    upstream = get_upstream_status()
    if upstream:
        upstream = {
            k: iso(v) if k in ("feed_time", "written", "checked_at") else v for k, v in upstream.items()
        }

    return {
        "feed_status": compute_feed_health(now),
        "server_time": iso(now),
        "last_synced_at": iso(last_success),
        "feed_generated_at": iso(feed_ts),
        "feed_age_seconds": int((now - feed_ts).total_seconds()) if feed_ts else None,
        "sync_interval_seconds": settings.BUS_SYNC_INTERVAL,
        "vehicle_positions": {
            "last_attempt_at": iso(vp_state.get("last_attempt_at")),
            "last_attempt_status": vp_state.get("last_attempt_status"),
            "consecutive_failures": vp_state.get("consecutive_failures", 0),
            "last_error": vp_state.get("last_error"),
            "last_error_at": iso(vp_state.get("last_error_at")),
            "total_vehicles": LiveBus.objects.count(),
            "unique_buses": LiveBus.objects.filter(is_superseded=False).count(),
            "gps_status_counts": gps_status_counts(now),
        },
        "trip_updates": {
            "last_synced_at": iso(tu_state.get("last_success_at")),
            "last_attempt_status": tu_state.get("last_attempt_status"),
            "feed_generated_at": iso(tu_state.get("feed_timestamp")),
            "total_trip_updates": tu_state.get("total_vehicles"),
        },
        "upstream": upstream,
        "gtfs": _gtfs_overview(),
        "thresholds": {
            "gps_live_max_age_seconds": settings.GPS_FRESHNESS["LIVE_MAX_AGE"],
            "gps_recent_max_age_seconds": settings.GPS_FRESHNESS["RECENT_MAX_AGE"],
            "gps_delayed_max_age_seconds": settings.GPS_FRESHNESS["DELAYED_MAX_AGE"],
            "feed_stale_after_seconds": settings.FEED_HEALTH["STALE_AFTER"],
            "feed_down_after_seconds": settings.FEED_HEALTH["DOWN_AFTER"],
        },
    }
