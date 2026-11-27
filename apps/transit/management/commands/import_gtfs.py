import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.transit.services import trip_context
from apps.transit.services.gtfs_import import GtfsImportError, download_gtfs, import_gtfs


class Command(BaseCommand):
    help = (
        "Import GTFS static data (routes, stops, trips, shapes). "
        "Reads a folder of .txt files, or downloads APSRTC_GTFS_URL with --download."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "path", nargs="?", default=None,
            help=f"Folder containing stops.txt, trips.txt, ... (default: GTFS_DIR = {settings.GTFS_DIR})",
        )
        parser.add_argument(
            "--download", action="store_true",
            help=f"Download and import {settings.APSRTC['GTFS_URL']} (for servers without the data folder)",
        )

    def handle(self, *args, **options):
        try:
            if options["download"]:
                url = settings.APSRTC["GTFS_URL"]
                with tempfile.TemporaryDirectory() as tmp:
                    self.stdout.write(f"Downloading {url} ...")
                    folder = download_gtfs(url, Path(tmp))
                    stats = self._import(folder, source_label=url)
            else:
                folder = Path(options["path"] or settings.GTFS_DIR)
                if not folder.is_dir():
                    raise CommandError(f"{folder} is not a folder (use --download to fetch the feed)")
                stats = self._import(folder)
        except GtfsImportError as exc:
            raise CommandError(str(exc)) from exc
        trip_context.reset_feed_memo()
        self.stdout.write(self.style.SUCCESS(
            f"Done in {stats.seconds}s: {stats.routes} routes, {stats.stops} stops, {stats.trips} trips, "
            f"{stats.patterns} patterns ({stats.approximate_patterns} with approximate paths), "
            f"{stats.shapes} shapes; {stats.interpolated_times} stop times interpolated, "
            f"{stats.skipped_trips} trips skipped."
        ))
        self.stdout.write("Running workers pick up the new data within 30 s. Live progress fills in on the next sync.")

    def _import(self, folder, source_label=""):
        self.stdout.write(f"Importing GTFS from {folder} ...")
        return import_gtfs(folder, source_label=source_label)
