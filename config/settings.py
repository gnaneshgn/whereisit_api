"""
VizagMove backend settings.

Everything environment-specific comes from environment variables (optionally
loaded from backend/.env). Local development works with zero infrastructure:
SQLite + file-based cache + Celery filesystem broker. Setting REDIS_URL and
DATABASE_URL switches to Redis and PostgreSQL without code changes.
"""
from pathlib import Path
import os

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_str(name, default=""):
    return os.environ.get(name, default).strip()


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default):
    value = os.environ.get(name, "").strip()
    try:
        return int(value) if value else default
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer, got {value!r}") from exc


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
DEBUG = os.getenv("DEBUG", "False").lower() == "true"



SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "dev-insecure-only-for-local-development"
    else:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DEBUG=False")

ALLOWED_HOSTS = env_list(
    "ALLOWED_HOSTS",
    "localhost,127.0.0.1,whereisitapi-production.up.railway.app"
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "corsheaders",
    # Local
    "apps.buses",
    "apps.transit",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Database: SQLite by default, PostgreSQL via DATABASE_URL
# ---------------------------------------------------------------------------
DATABASES = {
    "default": dj_database_url.parse(
        env_str("DATABASE_URL") or f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=env_int("DB_CONN_MAX_AGE", 60),
    )
}
if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3":
    # Celery worker and web server write concurrently; wait instead of failing.
    DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 20

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
# API timestamps are always UTC ("...Z"); the mobile app converts to local time.
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

VAR_DIR = BASE_DIR / "var"  # local runtime state (dev cache, dev broker)

# ---------------------------------------------------------------------------
# Cache: Redis when configured, otherwise a file cache shared by all processes
# (LocMemCache would NOT be shared between the Celery worker and the web server).
# ---------------------------------------------------------------------------
REDIS_URL = env_str("REDIS_URL")

if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": "vizagmove",
            "TIMEOUT": None,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
            "LOCATION": str(VAR_DIR / "cache"),
            "KEY_PREFIX": "vizagmove",
            "TIMEOUT": None,
            "OPTIONS": {"MAX_ENTRIES": 2000},
        }
    }

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"]
    + (["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    "DEFAULT_PAGINATION_CLASS": "common.pagination.StandardPagination",
    "PAGE_SIZE": 100,
    "EXCEPTION_HANDLER": "common.exceptions.api_exception_handler",
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": env_str("API_ANON_THROTTLE", "120/min")},
    "UNAUTHENTICATED_USER": None,
}
API_MAX_PAGE_SIZE = env_int("API_MAX_PAGE_SIZE", 1000)

# ---------------------------------------------------------------------------
# CORS (React Native itself does not need CORS; Expo web / browsers do)
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_ALL_ORIGINS = DEBUG and not CORS_ALLOWED_ORIGINS
CORS_ALLOW_METHODS = ["GET", "OPTIONS"]

# ---------------------------------------------------------------------------
# APSRTC feed integration
# ---------------------------------------------------------------------------
APSRTC = {
    "VEHICLE_POSITIONS_URL": env_str(
        "APSRTC_VEHICLE_POSITIONS_URL", "https://maps.reclaim.city/apsrtc/rt/vehicle_positions.json"
    ),
    "TRIP_UPDATES_URL": env_str(
        "APSRTC_TRIP_UPDATES_URL", "https://maps.reclaim.city/apsrtc/rt/trip_updates.json"
    ),
    "STATUS_URL": env_str("APSRTC_STATUS_URL", "https://maps.reclaim.city/apsrtc/rt/status.json"),
    "GTFS_URL": env_str("APSRTC_GTFS_URL", "https://maps.reclaim.city/gtfs/apsrtc.zip"),
    "CONNECT_TIMEOUT": env_int("APSRTC_CONNECT_TIMEOUT", 5),
    "READ_TIMEOUT": env_int("APSRTC_READ_TIMEOUT", 30),
    "MAX_RETRIES": env_int("APSRTC_MAX_RETRIES", 2),
    "USER_AGENT": env_str("APSRTC_USER_AGENT", "VizagMove-backend/1.0"),
    "STORE_RAW_VEHICLE_DATA": env_bool("STORE_RAW_VEHICLE_DATA", False),
}

BUS_SYNC_INTERVAL = env_int("BUS_SYNC_INTERVAL", 30)
TRIP_UPDATES_SYNC_INTERVAL = env_int("TRIP_UPDATES_SYNC_INTERVAL", 60)
FEED_STATUS_CHECK_INTERVAL = env_int("FEED_STATUS_CHECK_INTERVAL", 60)

GPS_FRESHNESS = {
    "LIVE_MAX_AGE": env_int("GPS_LIVE_MAX_AGE", 90),
    "RECENT_MAX_AGE": env_int("GPS_RECENT_MAX_AGE", 300),
    "DELAYED_MAX_AGE": env_int("GPS_DELAYED_MAX_AGE", 1800),
    "FUTURE_TOLERANCE": env_int("GPS_FUTURE_TOLERANCE", 120),
}
if not (
    0 < GPS_FRESHNESS["LIVE_MAX_AGE"] < GPS_FRESHNESS["RECENT_MAX_AGE"] < GPS_FRESHNESS["DELAYED_MAX_AGE"]
):
    raise ImproperlyConfigured("GPS freshness thresholds must satisfy 0 < LIVE < RECENT < DELAYED")

FEED_HEALTH = {
    "STALE_AFTER": env_int("FEED_STALE_AFTER", 300),
    "DOWN_AFTER": env_int("FEED_DOWN_AFTER", 600),
    "SYNC_LOG_RETENTION_DAYS": env_int("FEED_SYNC_LOG_RETENTION_DAYS", 3),
}


def _parse_bbox(raw):
    if not raw:
        return None
    try:
        min_lat, min_lon, max_lat, max_lon = (float(x) for x in raw.split(","))
    except ValueError as exc:
        raise ImproperlyConfigured("SERVICE_AREA_BBOX must be 'min_lat,min_lon,max_lat,max_lon'") from exc
    if not (min_lat < max_lat and min_lon < max_lon):
        raise ImproperlyConfigured("SERVICE_AREA_BBOX min values must be smaller than max values")
    return (min_lat, min_lon, max_lat, max_lon)


SERVICE_AREA_BBOX = _parse_bbox(env_str("SERVICE_AREA_BBOX"))

# ---------------------------------------------------------------------------
# GTFS static + trip progress (apps.transit)
# ---------------------------------------------------------------------------
GTFS_DIR = Path(env_str("GTFS_DIR") or BASE_DIR / "data" / "apsrtc")
TRANSIT = {
    # GPS further than this from the trip's road = off route (no stop is reported)
    "OFF_ROUTE_M": env_int("TRANSIT_OFF_ROUTE_M", 150),
    # Within this distance (along the road) of a stop = "at" that stop
    "AT_STOP_M": env_int("TRANSIT_AT_STOP_M", 60),
    # Journey planning: walking radius around a coordinate, and the radius that
    # groups an explicit stop with its twin across the road
    "JOURNEY_WALK_RADIUS_M": env_int("TRANSIT_JOURNEY_WALK_RADIUS_M", 500),
    "SAME_STOP_RADIUS_M": env_int("TRANSIT_SAME_STOP_RADIUS_M", 150),
    # Scheduled departures shown when no live bus is coming
    "SCHEDULE_WINDOW_MIN": env_int("TRANSIT_SCHEDULE_WINDOW_MIN", 180),
    # Trip-update predictions older than this are ignored for ETAs
    "TRIP_UPDATE_MAX_AGE": env_int("TRANSIT_TRIP_UPDATE_MAX_AGE", 900),
}

# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = env_str("CELERY_BROKER_URL") or REDIS_URL or "filesystem://"
if CELERY_BROKER_URL == "filesystem://":
    _broker_dir = VAR_DIR / "celery-broker"
    for _sub in ("in", "processed"):
        (_broker_dir / _sub).mkdir(parents=True, exist_ok=True)
    CELERY_BROKER_TRANSPORT_OPTIONS = {
        "data_folder_in": str(_broker_dir / "in"),
        "data_folder_out": str(_broker_dir / "in"),
        "processed_folder": str(_broker_dir / "processed"),
        "store_processed": False,
    }
CELERY_RESULT_BACKEND = None  # results are not needed; outcomes go to FeedSyncLog
CELERY_TASK_IGNORE_RESULT = True
CELERY_TIMEZONE = "UTC"
CELERY_ENABLE_UTC = True
CELERY_TASK_ACKS_LATE = False
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# Each periodic task expires after one interval: if the worker falls behind,
# stale queued runs are discarded instead of piling up.
CELERY_BEAT_SCHEDULE = {
    "sync-live-vehicle-positions": {
        "task": "apps.buses.tasks.sync_live_vehicle_positions",
        "schedule": float(BUS_SYNC_INTERVAL),
        "options": {"expires": BUS_SYNC_INTERVAL},
    },
    "sync-trip-updates": {
        "task": "apps.buses.tasks.sync_trip_updates",
        "schedule": float(TRIP_UPDATES_SYNC_INTERVAL),
        "options": {"expires": TRIP_UPDATES_SYNC_INTERVAL},
    },
    "check-feed-status": {
        "task": "apps.buses.tasks.check_feed_status",
        "schedule": float(FEED_STATUS_CHECK_INTERVAL),
        "options": {"expires": FEED_STATUS_CHECK_INTERVAL},
    },
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"standard": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "standard"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "apps": {"level": env_str("APP_LOG_LEVEL", "INFO"), "propagate": True},
        "django.db.backends": {"level": "WARNING"},
    },
}

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
