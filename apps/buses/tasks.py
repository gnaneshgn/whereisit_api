"""
Celery tasks. Thin wrappers: all logic lives in services.sync_service.

Overlap protection is layered:
  1. Beat sends each task with ``expires`` = its interval, so a backlog of
     stale runs is dropped by the worker instead of executed.
  2. sync_service holds a cache lock per feed, so two workers never sync the
     same feed concurrently.
Retries: HTTP-level retries with backoff happen inside the request
(APSRTC_MAX_RETRIES); beyond that, the next scheduled run is the retry.
Unexpected exceptions are retried once by Celery after a short delay.
"""
from celery import shared_task
from django.conf import settings

from apps.buses.services import sync_service

COMMON = dict(
    acks_late=False,
    ignore_result=True,
    autoretry_for=(ConnectionError,),
    max_retries=1,
    default_retry_delay=5,
)


@shared_task(
    name="apps.buses.tasks.sync_live_vehicle_positions",
    soft_time_limit=settings.BUS_SYNC_INTERVAL * 3,
    time_limit=settings.BUS_SYNC_INTERVAL * 4,
    **COMMON,
)
def sync_live_vehicle_positions():
    outcome = sync_service.sync_vehicle_positions()
    return {"status": outcome.status, "vehicles": outcome.total_vehicles}


@shared_task(
    name="apps.buses.tasks.sync_trip_updates",
    soft_time_limit=settings.TRIP_UPDATES_SYNC_INTERVAL * 2,
    time_limit=settings.TRIP_UPDATES_SYNC_INTERVAL * 3,
    **COMMON,
)
def sync_trip_updates():
    outcome = sync_service.sync_trip_updates()
    return {"status": outcome.status, "trip_updates": outcome.total_vehicles}


@shared_task(name="apps.buses.tasks.check_feed_status", soft_time_limit=60, time_limit=90, **COMMON)
def check_feed_status():
    outcome = sync_service.check_feed_status()
    return {"status": outcome.status, "upstream_vehicles": outcome.total_vehicles}
