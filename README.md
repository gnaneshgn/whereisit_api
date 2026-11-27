# VizagMove backend

Django REST Framework backend that tracks APSRTC buses in real time using the
GTFS-Realtime feeds published at `maps.reclaim.city`, and serves them to the
VizagMove React Native app.

**Status:** Phases 1–2 are complete: live tracking, GTFS static import, stop-by-stop trip
progress with ETAs, route and stop APIs, and source → destination journey planning on direct buses.

---

## Quick start (local, no Redis needed)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env              # then set DJANGO_SECRET_KEY, and REDIS_URL= (empty) if Redis isn't running
python manage.py migrate
python manage.py import_gtfs        # routes/stops/trips from data/apsrtc (~3.5 min, ~350 MB RAM)
python manage.py sync_live_feed     # one real sync
python manage.py runserver
```

No `data/` folder (e.g. on a server)? `python manage.py import_gtfs --download` fetches
`APSRTC_GTFS_URL` (21 MB zip) and imports it. Re-run either command whenever the feed is
updated; the swap is atomic and running workers pick it up within 30 s.

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
| Static GTFS | `/gtfs/apsrtc.zip` | Same publisher as the realtime feed; local copy in `data/apsrtc/` |

Facts from the real data that shaped the design:

- **`vehicle.vehicle.id` is a per-trip key** (`24112026_A033_11_WALTAIR`), not a bus id.
  The **registration number** is in `licensePlate` / `label`.
- **The same bus can appear more than once** (about 700 plates have older trip records
  still in the feed). The newest record per plate is primary; the others are flagged
  `is_superseded` and hidden from `/live/`.
- **~18% of vehicles have no `trip`**, so `route_id` / `trip_id` are null for them.
- **`routeId` is an internal slug** (`b011-rtc-complex-vsp-simhachalam`), *not* the
  public route number. `route_number` (the APSRTC route code, e.g. `A009`) and
  `destination` come from the imported GTFS; without it they stay `null`.
- **Some plates are partial or empty** (`39Z0142`, `""`). They are kept as-is or set to null, never "fixed".
- **Some GPS timestamps are years in the future.** They are discarded (`gps_timestamp: null`, status `UNAVAILABLE`).
- Timestamps are **strings** of epoch seconds. Speed is in **m/s** (also exposed as `speed_kmh`).

---

## GTFS static data: which dataset and why

`data/` holds two GTFS feeds. Measured against the live feed (27 Nov 2026):

| Folder | Publisher | Live `trip_id` match | Live `route_id` match | Notes |
|---|---|---|---|---|
| `data/apsrtc` | reclaim.city (same as the realtime feed) | **1625 / 1625** | **1625 / 1625** | 5,091 routes, 11,156 stops, 54,812 trips, shapes for 99% of trips, valid to Oct 2027. Already includes the 462 Neo2308 routes |
| `data/gtfs` | Neo2308 community feed | 0 / 1625 | 0 / 1625 | Numeric ids, no names, expired 30 Sep 2026. **Not used** |

Other facts that shaped the design:

- Trip-update stop ids are all in `stops.txt` (1574/1574), and their `stop_sequence` matches `stop_times.txt` 99.7% of the time.
- Shapes are sparse, with a median of **315 m between points**, so positions are projected onto segments, never snapped to vertices.
- Running time varies by up to 25 min between trips on the same stop list, so **each trip keeps its own timings**.
- Times past `24:00:00` are used for trips that run past midnight (23k rows).
- **`trip_headsign` is unreliable:** about 24% of headsigns name neither end of the trip (an Airport-bound AM10 trip is signed "Tagarapuvalasa"). `destination` is therefore the trip's **last stop**, and the raw headsign is only informational.
- Route codes (`A009`, `9092`, …) are the only route numbers in the data.

---

## Trip tracking algorithm (`apps/transit/services/`)

**Import (`gtfs_import.py`, run once per feed):**

1. Trips are grouped into **patterns**: route + shape + exact ordered stop list. 54,812 trips collapse into 12,093 patterns.
2. Each pattern's stops are placed on its road shape as a **distance along the road**. Each stop is searched only *ahead* of the previous one, so loops and out-and-back roads come out right.
3. If a pattern has no shape, or its shape doesn't fit the stops (median offset over 300 m), the path is the line joining the stops and the pattern is flagged `path_approximate` (476 patterns).
4. Missing stop times are interpolated by distance between timed neighbours, never extrapolated.

**Live, at every sync (`progress_service.py`, about 2 s for ~1,600 buses):**

1. The live record's `trip_id` gives the exact stops and road. **Direction is never guessed.**
2. The GPS point is projected onto the road. Every place the road passes within 150 m is a candidate. The candidate with the lowest score is chosen:
   - distance from the road
   - plus a penalty when the bus heading disagrees with the road direction
   - plus a penalty for moving backwards since the last fix
   - plus a penalty for falling outside the window implied by the realtime trip update
3. Comparing the bus's distance along the road with the stops' distances gives the **last passed, current (within 60 m) and next stop**, the stops remaining and the % complete.
4. If no part of the road is within 150 m, the status is **`off_route`** and no stop is reported.
5. Progress is shown only while the GPS is LIVE or RECENT (≤ 5 min).

**ETA for each stop ahead:**
- **Realtime:** the trip update's prediction, then the trip's own scheduled gaps added for later stops (delay propagation).
- **Schedule:** otherwise, GPS time + (scheduled time at the stop − scheduled time at the bus's position). This assumes the bus keeps its current delay.
- If there's neither, the ETA is null. Every ETA carries `eta_source`.

**Measured on the real feed:**
- Next stop agrees with APSRTC's independent trip-update prediction: **95.2% exactly, 99.5% within one stop** (1,215 buses).
- GPS-to-road distance: median 7 m, p90 28 m.
- About 8% of buses are `off_route`, with a median of 850 m from their trip's road. They are on detours, mis-assigned to a trip, or heading to the depot.

**Journeys (`journey_service.py`):**
1. Each end becomes candidate stops:
   - an explicit stop, plus its twin across the road within 150 m, or
   - every stop within 500 m of a coordinate.
2. A pattern serves the trip when a boarding stop comes **before** an alighting stop in its stop list.
3. For each matching pattern you get:
   - the stops in between, the distance and the scheduled ride time
   - **live buses that haven't reached the boarding stop yet**, with ETAs at both ends
   - the next scheduled departures (labelled `source: "schedule"`)
4. Only direct buses are planned; journeys that need a change are not supported yet.

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
  apps/transit/      GTFS static + trip progress + journeys
    models.py        GtfsFeed, Stop, Route, Shape, Pattern, PatternStop, Trip
    services/
      gtfs_import.py       parse, patterns, place stops on shapes, atomic replace, --download
      geometry.py          encoded polylines, segment projection, slicing (pure Python)
      trip_context.py      per-trip stops/path/timings, memoised per feed
      progress_service.py  where is the bus on its trip + ETAs
      live_bridge.py       called by the sync: labels + progress per vehicle
      timeline_service.py  stop timeline for one bus
      journey_service.py   source -> destination, stop arrivals
    management/commands/import_gtfs.py
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

## API

All responses are JSON. Datetimes are UTC (`...Z`). Unknown values are `null`.

**Buses (live feed + GTFS labels/progress)**

| Method | Path | Description |
|---|---|---|
| GET | `/api/buses/live/` | One record per physical bus with a usable GPS fix (not `UNAVAILABLE`), freshest first |
| GET | `/api/buses/` | Every vehicle in the feed, including `UNAVAILABLE` ones |
| GET | `/api/buses/search/?q=` | Registration (spaces and case ignored), vehicle id, route id, route code, or a place in the route name or destination (3+ characters). Exact plate first |
| GET | `/api/buses/nearby/?latitude=&longitude=&radius=&limit=` | Buses with a usable fix within `radius` km (default 5, max 50), nearest first, with `distance_km` |
| GET | `/api/buses/{vehicle_id}/` | Detail by vehicle id **or** registration. Adds `upcoming_stops` (with `distance_km`, `eta`) and `route` (every stop with `status`, plus `path`) |
| GET | `/api/buses/{vehicle_id}/trip/?to_stop=&from_stop=` | Live trip timeline and `path_ahead` (the road still to travel). With `to_stop`, also `journey`: stops left, distance and ETA to the rider's stop |
| GET | `/api/routes/{route_id}/buses/` | Live buses on a route (live feed only) |

**Routes, stops, trips, journeys (need `import_gtfs`; answer `501 not_available` before it)**

| Method | Path | Description |
|---|---|---|
| GET | `/api/routes/?q=&service_type=city\|intercity\|other&bbox=` | Routes in the service area, with `active_buses` |
| GET | `/api/routes/search/?q=` | Route code (`A009`), route name, or any stop on the route (`Simhachalam`) |
| GET | `/api/routes/{route_id}/?pattern_id=` | Route, its `patterns` (each direction or variant), and the chosen pattern's ordered `stops` and `path` |
| GET | `/api/routes/{route_id}/stops/?pattern_id=` | Ordered stops (default: the busiest pattern) |
| GET | `/api/stops/nearby/?latitude=&longitude=&radius=1&limit=20` | Nearest stops (radius in km, max 5), each with the `routes` serving it |
| GET | `/api/stops/search/?q=` | Stop names, exact and prefix matches first |
| GET | `/api/stops/{stop_id}/` | Stop and its routes |
| GET | `/api/stops/{stop_id}/arrivals/` | Live buses heading to the stop: `stops_away`, `distance_to_board_km`, `eta_at_board` |
| GET | `/api/journeys/?from_stop=&to_stop=` (or `from_lat`/`from_lon`/`to_lat`/`to_lon`, `include_path`) | Direct buses from source to destination (see below) |
| GET | `/api/trips/{trip_id}/` | Scheduled stop times (service-day `HH:MM`) and path |

**Feed**

| Method | Path | Description |
|---|---|---|
| GET | `/api/feed/status/` | Feed health, sync times, GPS status counts, upstream status, `gtfs` block, thresholds |
| GET | `/api/health/` | Liveness probe |

Bus fields from GTFS (all `null` when unknown):

| Field | Meaning |
|---|---|
| `route_number`, `route_name` | APSRTC route code and name |
| `start_point`, `destination` | First and last stop of the bus's trip |
| `last_stop`, `current_stop`, `next_stop` | `{stop_id, stop_name, stop_sequence}`. `next_stop` adds `distance_m`, `expected_arrival` and `eta_source`. `current_stop` is set only when the bus is within 60 m |
| `current_location` | `"At X"`, or `"Between X and Y"` |
| `trip_progress` | `{status: in_transit\|at_stop\|not_started\|completed\|off_route, progress_pct, stops_remaining, distance_along_m, route_length_m, path_approximate, as_of}` |

Journey option:

```json
{"route": {"route_number": "SK31", "route_name": "...", "service_type": "city", "bus_type": "City Ordinary"},
 "towards": "Simhachalam Depot", "headsign": "...",
 "board": {"stop_id": "232186", "stop_name": "Rtc Complex Vsp", "walk_m": 0},
 "alight": {"stop_id": "13801", "stop_name": "Simhachalam", "walk_m": 0},
 "stop_count": 8, "stops": [{"stop_name": "Rtc Complex Vsp", "distance_from_board_km": 0.0}, "..."],
 "ride_distance_km": 16.34, "scheduled_ride_minutes": 37,
 "live_buses": [{"registration_number": "AP39Z0475", "stops_away": 2, "distance_to_board_km": 1.8,
                 "eta_at_board": "...Z", "eta_at_alight": "...Z", "eta_source": "realtime"}],
 "next_scheduled": [{"trip_id": "...", "departs_at": "...Z", "arrives_at": "...Z", "source": "schedule"}],
 "path_approximate": false}
```

Use `towards` (the bus's last stop) to tell riders which bus to board. `headsign` is the raw
GTFS text and often names a different place.

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

Bus detail also includes `trip_start_date`, `is_superseded`, `last_synced_at` and the raw
`trip_update` block (`next_stop.stop_id`, `stop_sequence`, `expected_arrival`, `delay_seconds`).

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
| `GTFS_DIR` | `data/apsrtc` | Folder read by `import_gtfs` |
| `APSRTC_GTFS_URL` | reclaim.city zip | Used by `import_gtfs --download` |
| `TRANSIT_OFF_ROUTE_M` / `TRANSIT_AT_STOP_M` | 150 / 60 | Off-route distance and the "at the stop" radius |
| `TRANSIT_JOURNEY_WALK_RADIUS_M` / `TRANSIT_SAME_STOP_RADIUS_M` | 500 / 150 | Journey stop matching |
| `TRANSIT_SCHEDULE_WINDOW_MIN` / `TRANSIT_TRIP_UPDATE_MAX_AGE` | 180 / 900 | Scheduled departures window, and the maximum age of a realtime prediction |

---

## Production deployment

1. Set `DEBUG=False`, `DJANGO_SECRET_KEY`, `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`.
2. Provision PostgreSQL and Redis. Set `DATABASE_URL` and `REDIS_URL`, and add
   `psycopg2-binary` to `requirements.txt`.
3. `python manage.py migrate && python manage.py collectstatic --noinput && python manage.py import_gtfs --download`
   (`data/` is not in git. The import needs about 350 MB of RAM and takes 3–4 min; repeat it when reclaim.city publishes a new feed.)
4. Run these processes (systemd, supervisor or containers):
   - `gunicorn config.wsgi:application --workers 3 --bind 0.0.0.0:8000`
   - `celery -A config worker -l INFO --concurrency 2`
   - `celery -A config beat -l INFO`. Run **exactly one** beat instance.
5. Put a TLS-terminating reverse proxy in front (it should send `X-Forwarded-Proto`).
6. Monitor `/api/feed/status/`: alert when `feed_status` is `down` or `stale`.

---

## Roadmap

- **Done:** GTFS import, route and stop APIs, trip progress and ETAs, journeys on direct buses, stop arrivals.
- **Next:**
  - journeys with one change
  - places along the remaining route (OpenStreetMap POIs within ~400 m of `path_ahead`)
  - crowd levels from opt-in rider pings
  - a scheduled `import_gtfs --download`
  - PostgreSQL index tuning
