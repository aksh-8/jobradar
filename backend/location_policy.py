"""Strict United States eligibility and location-priority policy."""

from __future__ import annotations

import re
from enum import Enum


class UsLocationTier(str, Enum):
    LOS_ANGELES = "Los Angeles priority"
    REMOTE = "Remote — United States"
    CALIFORNIA = "California"
    EAST_COAST = "East Coast"
    REST_OF_US = "United States"


_TIER_PRIORITY = {
    UsLocationTier.LOS_ANGELES: 0,
    UsLocationTier.REMOTE: 1,
    UsLocationTier.CALIFORNIA: 2,
    UsLocationTier.EAST_COAST: 3,
    UsLocationTier.REST_OF_US: 4,
}

_STATE_ABBREVIATIONS = (
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi",
    "id", "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi",
    "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc",
    "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut",
    "vt", "va", "wa", "wv", "wi", "wy", "dc",
)
_STATE_ABBREVIATION_PATTERN = re.compile(
    r"(?:,\s*|\()" + r"(?:" + "|".join(_STATE_ABBREVIATIONS) + r")\b",
    re.IGNORECASE,
)
_STATE_NAMES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming", "district of columbia",
)
_LOS_ANGELES_MARKERS = (
    "los angeles", "greater los angeles", "santa monica", "culver city",
    "el segundo", "playa vista", "marina del rey", "beverly hills",
    "west hollywood", "burbank", "glendale", "pasadena", "long beach",
    "hawthorne", "torrance", "manhattan beach",
)
_EAST_COAST_NAMES = (
    "maine", "new hampshire", "vermont", "massachusetts", "rhode island",
    "connecticut", "new york", "new jersey", "pennsylvania", "delaware",
    "maryland", "district of columbia", "washington, dc", "virginia",
    "north carolina", "south carolina", "georgia", "florida",
)
_EAST_COAST_ABBREVIATIONS = re.compile(
    r"(?:,\s*|\()(?:me|nh|vt|ma|ri|ct|ny|nj|pa|de|md|dc|va|nc|sc|ga|fl)\b",
    re.IGNORECASE,
)
def classify_us_location(
    location: str | None,
    workplace_type: str | None = None,
    description: str | None = None,
) -> UsLocationTier | None:
    """Return the preferred US tier, or ``None`` when US eligibility is unverified."""

    rendered = _normalized(location)
    workplace = _normalized(workplace_type)
    context = _normalized(description)
    remote = "remote" in rendered or "remote" in workplace
    explicit_us = _explicit_us_location(rendered)

    if not explicit_us and remote:
        explicit_us = any(
            marker in context
            for marker in (
                "remote within the united states",
                "remote in the united states",
                "remote, united states",
                "remote - united states",
                "remote in the u.s.",
                "remote within the u.s.",
                "us remote",
                "u.s. remote",
            )
        )
    if not explicit_us:
        return None

    if any(marker in rendered for marker in _LOS_ANGELES_MARKERS):
        return UsLocationTier.LOS_ANGELES
    if remote:
        return UsLocationTier.REMOTE
    if "california" in rendered or re.search(r"(?:,\s*|\()ca\b", rendered):
        return UsLocationTier.CALIFORNIA
    if any(marker in rendered for marker in _EAST_COAST_NAMES) or (
        _EAST_COAST_ABBREVIATIONS.search(rendered)
    ):
        return UsLocationTier.EAST_COAST
    return UsLocationTier.REST_OF_US


def is_us_based(
    location: str | None,
    workplace_type: str | None = None,
    description: str | None = None,
) -> bool:
    return classify_us_location(location, workplace_type, description) is not None


def us_location_sort_key(
    location: str | None,
    workplace_type: str | None = None,
    description: str | None = None,
) -> tuple[int, str]:
    tier = classify_us_location(location, workplace_type, description)
    priority = _TIER_PRIORITY[tier] if tier is not None else 99
    return priority, _normalized(location)


def _explicit_us_location(location: str) -> bool:
    if not location:
        return False
    if "united states" in location or re.search(r"\busa\b", location):
        return True
    if re.search(r"\bu\.s\.(?:\s|$)", location):
        return True
    if location in {"us", "u.s."}:
        return True
    if _STATE_ABBREVIATION_PATTERN.search(location):
        return True
    return any(state in location for state in _STATE_NAMES)


def _normalized(value: str | None) -> str:
    return " ".join((value or "").casefold().split())
