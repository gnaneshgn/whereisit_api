"""
Run feed synchronization without Celery.

    python manage.py sync_live_feed            # one pass of all three feeds
    python manage.py sync_live_feed --loop     # keep syncing every BUS_SYNC_INTERVAL seconds

Useful for local development and for debugging the pipeline. In production
Celery Beat schedules the same service functions.
"""
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.buses.services import sync_service


class Command(BaseCommand):
    help = "Synchronize APSRTC vehicle positions, trip updates and feed status."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Run continuously.")
        parser.add_argument(
            "--only", choices=["vehicles", "trips", "status"], help="Sync a single feed only."
        )

    def handle(self, *args, loop=False, only=None, **options):
        interval = settings.BUS_SYNC_INTERVAL
        last_run = {"trips": 0.0, "status": 0.0}
        try:
            while True:
                started = time.monotonic()
                self._run_once(only, last_run, force=not loop)
                if not loop:
                    return
                time.sleep(max(1.0, interval - (time.monotonic() - started)))
        except KeyboardInterrupt:
            self.stdout.write("stopped")

    def _run_once(self, only, last_run, force):
        now = time.monotonic()
        jobs = []
        if only in (None, "vehicles"):
            jobs.append(("vehicle_positions", sync_service.sync_vehicle_positions))
        if only in (None, "trips") and (force or now - last_run["trips"] >= settings.TRIP_UPDATES_SYNC_INTERVAL):
            jobs.append(("trip_updates", sync_service.sync_trip_updates))
            last_run["trips"] = now
        if only in (None, "status") and (force or now - last_run["status"] >= settings.FEED_STATUS_CHECK_INTERVAL):
            jobs.append(("status", sync_service.check_feed_status))
            last_run["status"] = now

        for name, job in jobs:
            outcome = job()
            style = self.style.SUCCESS if outcome.status in ("success", "not_modified") else self.style.WARNING
            self.stdout.write(style(f"{name}: {outcome.status} count={outcome.total_vehicles} {outcome.message}"))
