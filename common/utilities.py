"""Small, dependency-free helpers shared across apps."""
import math
import re
from datetime import datetime, timezone as dt_timezone

from django.utils import timezone

_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def utc_now():
    return timezone.now()


def iso_utc(value):
    """datetime or ISO string -> '2026-11-24T10:30:00Z' (None passes through)."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(dt_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_float(value):
    """Convert to a finite float or return None (no silent zeros)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def to_int(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def epoch_to_datetime(value):
    """POSIX seconds (int or numeric string, as GTFS-RT JSON uses) -> aware UTC datetime."""
    seconds = to_int(value)
    if seconds is None or seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=dt_timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def clean_str(value):
    """Strip strings; empty / non-string values become None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_registration(value):
    """'ap 31 z-0475' -> 'AP31Z0475'. Returns None when nothing is left."""
    if value is None:
        return None
    normalized = _NON_ALNUM.sub("", str(value).upper())
    return normalized or None


def is_valid_coordinate(latitude, longitude):
    if latitude is None or longitude is None:
        return False
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return False
    # (0, 0) is the classic "no fix" placeholder, never a real APSRTC position.
    return not (latitude == 0 and longitude == 0)
