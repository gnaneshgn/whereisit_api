"""
Public bus numbers riders know (6A, 38K) for APSRTC schedule codes (B011, JN01).

The GTFS only carries schedule codes. The same code is reused by other depots
(B011 is also Motu – Vijayawada, Kotturu – Palakonda, ...), so a number is only
applied to City services; intercity routes keep their code.

Add entries to ``settings.LOCAL_ROUTE_NUMBERS`` as {"CODE": "NUMBER"}.
"""
from __future__ import annotations

from django.conf import settings
from django.db.models import Q

CITY = "city"


def _mapping() -> dict:
    return {k.strip().upper(): v.strip() for k, v in getattr(settings, "LOCAL_ROUTE_NUMBERS", {}).items()}


def display_number(code, service_type=None):
    """Rider-facing number for a route code, or the code itself."""
    if not code:
        return None
    if service_type == CITY:
        return _mapping().get(code.strip().upper(), code)
    return code


def display_for(route):
    return display_number(route.short_name, route.service_type)


def codes_for(text: str) -> list[str]:
    """Schedule codes whose public number matches ``text`` ("6a" -> ["B011"])."""
    wanted = (text or "").strip().lower()
    return [code for code, number in _mapping().items() if number.lower() == wanted]


def numbers_for(text: str) -> list[str]:
    """Public numbers for a schedule code ("b011" -> ["6A"])."""
    number = _mapping().get((text or "").strip().upper())
    return [number] if number else []


def route_query(text: str) -> Q:
    """Extra Route filter so searching a public number finds its city routes."""
    codes = codes_for(text)
    if not codes:
        return Q(pk__in=[])
    return Q(short_name__in=codes, service_type=CITY)
