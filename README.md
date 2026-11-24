# VizagMove backend

Django REST Framework backend that tracks APSRTC buses in real time using the
GTFS-Realtime feeds published at `maps.reclaim.city`, and serves them to the
VizagMove React Native app.

**Status:** Phase 1 is complete, plus the bus search, nearby buses and route-buses
endpoints the mobile app needs. GTFS static import and the route and stop APIs come in Phase 2.

---

## Quick start (local, no Redis needed)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env              # then set DJANGO_SECRET_KEY, and REDIS_URL= (empty) if Redis isn't running
python manage.py migrate
python manage.py sync_live_feed     # one real sync (about 5 s, ~6,600 vehicles)
python manage.py runserver
```

Open http://127.0.0.1:8000/api/buses/live/?bbox=17.60,83.00,17.95,83.45 to see live buses in Visakhapatnam.

### Connecting the mobile app

1. Run the server on all interfaces: `python manage.py runserver 0.0.0.0:8000`.
2. Add your machine's LAN IP (and `10.0.2.2` for the Android emulator) to `ALLOWED_HOSTS` in `backend/.env`.
3. Set `API_BASE_URL=http://<LAN-IP>:8000/api` in `mobile/.env` and rebuild the app (`react-native-dotenv` values are baked in at build time).
4. If the phone can't connect, allow inbound TCP 8000 for Python in Windows Firewall (Private network).

### Keeping data fresh

Pick one of these:

| Mode | Command(s) | When |
|---|---|---|
| Simple loop | `python manage.py sync_live_feed --loop` | Local development |
| Celery (prod-like) | `celery -A config worker --pool=solo -l INFO` **and** `celery -A config beat -l INFO` | Staging / production |

`--pool=solo` is required on Windows; on Linux use the default prefork pool.

With `REDIS_URL` empty, Django uses a file cache under `var/cache/` and Celery
uses a filesystem broker under `var/celery-broker/` (on Windows this needs
`pywin32`, which `requirements.txt` installs). Set `REDIS_URL` to switch both to Redis.

### Tests

```powershell
python manage.py test apps
```

Every external call is mocked, so the tests never touch the real APSRTC service.

---

## Upstream feeds (inspected 24 Nov 2026)

| Feed | URL | Notes |
|---|---|---|
| Vehicle positions | `/apsrtc/rt/vehicle_positions.json` | GTFS-RT JSON, ~2.5 MB, ~6,600–6,900 vehicles, `FULL_DATASET`, refreshes ≈ every 30 s |
| Trip updates | `/apsrtc/rt/trip_updates.json` | ~4.7 MB, ~5,400 trips; `vehicle.id` matches vehicle positions |
| Status | `/apsrtc/rt/status.json` | `{network, feed_time, written, vehicles, on_trip, on_route, trip_updates}` |
| Static GTFS | `/gtfs/apsrtc.zip` | Phase 2 |

Facts from the real data that shaped the design:

- **`vehicle.vehicle.id` is a per-trip key** (`24112026_A033_11_WALTAIR`), not a bus id.
  The **registration number** is in `licensePlate` / `label`.
- **The same bus can appear more than once** (about 700 plates have older trip records
  still in the feed). The newest record per plate is primary; the others are flagged
  `is_superseded` and hidden from `/live/`.
- **~18% of vehicles have no `trip`**, so `route_id` / `trip_id` are null for them.
- **`routeId` is an internal slug** (`b011-rtc-complex-vsp-simhachalam`), *not* the
  public route number. `route_number` and `destination` stay `null` until GTFS
  mapping is added in Phase 2. They are never guessed.
- **Some plates are partial or empty** (`39Z0142`, `""`). They are kept as-is or set to null, never "fixed".
- **Some GPS timestamps are years in the future.** They are discarded (`gps_timestamp: null`, status `UNAVAILABLE`).
- Timestamps are **strings** of epoch seconds. Speed is in **m/s** (also exposed as `speed_kmh`).

---

## Architecture

```
backend/
  config/            settings (env-driven), urls, celery, asgi/wsgi
  common/            pagination envelope, error envelope, utilities
  apps/buses/
    models.py        LiveBus (current state, 1 row per vehicle), FeedSyncLog
    services/
      apsrtc_service.py        HTTP (retries, timeouts, ETag) + defensive parsing
      sync_service.py          fetch → validate → match → upsert → log, with locking
      bus_matching_service.py  registration de-duplication, route mapping hook, trip-update lookup
      feed_status_service.py   GPS freshness, feed health, sync bookkeeping
      location_service.py      bounding-box filtering (nearby search in Phase 3)
    selectors.py     read-side querysets used by views
    serializers.py   output + query-parameter validation
    views.py         thin DRF views
    tasks.py         Celery tasks (thin wrappers)
    management/commands/sync_live_feed.py
```

### Sync pipeline

1. Beat triggers `sync_live_vehicle_positions` every `BUS_SYNC_INTERVAL` (30 s).
   Each run **expires** after one interval, so a backlog is dropped instead of piling up.
2. A cache lock (Redis `SET NX`) prevents overlapping runs.
3. The fetch sends `If-None-Match`; HTTP retries use exponential backoff on connection errors and 429/5xx.
4. The payload is validated. **Invalid JSON, empty snapshots and snapshots older than
   the stored one are rejected, and the last good data stays in place.**
5. Rows are upserted in bulk. Vehicles missing from the full dataset are deleted.
   There is no per-update history, so the table stays at about 7k rows.
6. The outcome is written to `FeedSyncLog` (pruned after `FEED_SYNC_LOG_RETENTION_DAYS`)
   and to the cached feed state.

`sync_trip_updates` (60 s) caches next-stop predictions per vehicle.
`check_feed_status` (60 s) caches `status.json` and prunes old logs.

### Freshness

The **vehicle's own GPS timestamp** decides its status. A recent successful sync does not.

| Status | GPS age (default, configurable) |
|---|---|
| `LIVE` | ≤ 90 s |
| `RECENT` | ≤ 300 s |
| `DELAYED` | ≤ 1800 s |
| `UNAVAILABLE` | older, missing, or implausibly in the future |

Status is computed **at request time**, so it never shows a stale label between syncs.

Feed-level `feed_status`:

| Value | Meaning |
|---|---|
| `healthy` | Recent successful sync and upstream data is fresh. |
| `stale` | Sync works, but upstream stopped advancing (> `FEED_STALE_AFTER`). |
| `degraded` | The latest attempt failed; serving the last good data. |
| `down` | No successful sync for `FEED_DOWN_AFTER`. |
| `unknown` | Never synced. |

### Caching

- Last good normalized snapshot, per-feed sync state, ETags, upstream status and the
  trip-update map are kept in the cache (Redis in production).
- `/api/buses/live/` responses are cached for 10 s, keyed by data version and query string.
  Each successful sync bumps the version.

---

## API (Phase 1)

All responses are JSON. Datetimes are UTC (`...Z`). Unknown values are `null`.

| Method | Path | Description |
|---|---|---|
| GET | `/api/buses/live/` | One record per physical bus with a usable GPS fix (not `UNAVAILABLE`), freshest first |
| GET | `/api/buses/` | Every vehicle in the feed, including `UNAVAILABLE` ones |
| GET | `/api/buses/search/?q=` | Registration (spaces/case ignored), vehicle id, route id or route number; exact plate first |
| GET | `/api/buses/nearby/?latitude=&longitude=&radius=&limit=` | Buses with a usable fix within `radius` km (default 5, max 50), nearest first, with `distance_km` |
| GET | `/api/buses/{vehicle_id}/` | Detail by upstream vehicle id **or** registration number (returns the newest record) |
| GET | `/api/routes/{route_id}/buses/` | Live buses on an upstream route id (live feed only, no GTFS needed) |
| GET | `/api/feed/status/` | Feed health, sync times, GPS status counts, upstream status, thresholds |
| GET | `/api/health/` | Liveness probe |
| GET | `/api/routes/`, `/api/routes/search/`, `/api/routes/{id}/`, `/api/routes/{id}/stops/`, `/api/stops/nearby/` | **501 `not_available`** until GTFS import (Phase 2). The mobile app treats this as "not available yet", not as an error |

Query parameters for the list endpoints:

| Param | Example | Notes |
|---|---|---|
| `status` | `LIVE,RECENT` | Filter by GPS status |
| `route_id` | `b011-rtc-complex-vsp-simhachalam` | Upstream route id |
| `bbox` | `17.60,83.00,17.95,83.45` | `min_lat,min_lon,max_lat,max_lon` |
| `include_superseded` | `true` | `/api/buses/` only |
| `page`, `page_size` | `page_size=500` | Default 100, max `API_MAX_PAGE_SIZE` (1000) |

List response:

```json
{
  "success": true,
  "last_synced_at": "2026-11-24T13:50:57Z",
  "feed_status": "healthy",
  "count": 529, "page": 1, "page_size": 100, "total_pages": 6,
  "next": "...", "previous": null,
  "results": [{
    "vehicle_id": "24112026_A033_11_WALTAIR",
    "registration_number": "AP27Z0208",
    "route_id": "a033-bheemunipatnam-railway-station-vsp",
    "route_number": null,
    "trip_id": "A033_11_WALTAIR",
    "latitude": 17.74964, "longitude": 83.3491,
    "bearing": 154.7, "speed": 6.19, "speed_kmh": 22.3,
    "gps_timestamp": "2026-11-24T13:50:48Z", "gps_age_seconds": 20,
    "gps_status": "LIVE",
    "current_stop": null, "destination": null
  }]
}
```

Bus detail also includes `trip_start_date`, `is_superseded`, `last_synced_at` and a
`trip_update` block (`next_stop.stop_id`, `stop_sequence`, `expected_arrival`, `delay_seconds`).
Stop names arrive with GTFS in Phase 2.

Errors:

```json
{"success": false, "error": {"code": "invalid_input", "message": "Invalid request parameters.",
  "details": {"status": ["Unknown status FAST. Allowed: LIVE, RECENT, DELAYED, UNAVAILABLE"]}}}
```

---

## Configuration

All settings come from environment variables. See [.env.example](.env.example).
The important ones:

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | — | Required when `DEBUG=False` |
| `DATABASE_URL` | SQLite | e.g. `postgres://user:pass@host:5432/vizagmove` |
| `REDIS_URL` | empty → file cache + filesystem broker | Cache and Celery broker |
| `BUS_SYNC_INTERVAL` | 30 | Seconds between vehicle syncs |
| `GPS_LIVE_MAX_AGE` / `GPS_RECENT_MAX_AGE` / `GPS_DELAYED_MAX_AGE` | 90 / 300 / 1800 | Freshness thresholds |
| `SERVICE_AREA_BBOX` | empty | Restrict every API response to an area (e.g. Vizag) |
| `APSRTC_*_TIMEOUT`, `APSRTC_MAX_RETRIES` | 5 / 30 / 2 | HTTP behaviour |

---

## Production deployment

1. Set `DEBUG=False`, `DJANGO_SECRET_KEY`, `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`.
2. Provision PostgreSQL and Redis. Set `DATABASE_URL` and `REDIS_URL`, and add
   `psycopg2-binary` to `requirements.txt`.
3. `python manage.py migrate && python manage.py collectstatic --noinput`
4. Run these processes (systemd, supervisor or containers):
   - `gunicorn config.wsgi:application --workers 3 --bind 0.0.0.0:8000`
   - `celery -A config worker -l INFO --concurrency 2`
   - `celery -A config beat -l INFO`. Run **exactly one** beat instance.
5. Put a TLS-terminating reverse proxy in front (it should send `X-Forwarded-Proto`).
6. Monitor `/api/feed/status/`: alert when `feed_status` is `down` or `stale`.

---

## Roadmap

- **Phase 2:** `import_gtfs` command; BusRoute, BusStop, BusTrip, BusStopTime and BusShape models;
  `route_number` and `destination` mapping (`bus_matching_service.lookup_route_metadata`);
  route and stop APIs; trip details endpoint.
- **Phase 3:** search (registration, vehicle id, route number), nearby buses and stops.
- **Phase 4:** further caching optimization and PostgreSQL index tuning.
