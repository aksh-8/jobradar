"""Discovery preferences are ranking signals, not invented eligibility facts."""
from datetime import datetime, timezone
import os

from backend.location_policy import us_location_sort_key
from backend.role_targeting import level_penalty


def posting_age_hours(value: str | None) -> float | None:
    if not value:
        return None
    try:
        if value.isdigit():
            timestamp = float(value)
            posted = datetime.fromtimestamp(timestamp / 1000 if timestamp > 1e11 else timestamp,
                                           tz=timezone.utc)
        else:
            posted = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - posted).total_seconds() / 3600
        return age if age >= 0 else None
    except (ValueError, OverflowError, OSError):
        return None


def priority_key(result):
    age = posting_age_hours(result.date_posted)
    company = (result.company or "").casefold()
    targets = os.getenv("PRIORITY_COMPANIES", "Apple|Google|Microsoft|Amazon|Meta|NVIDIA|Tesla")
    preferred = company in {name.strip().casefold() for name in targets.split("|")}
    return (
        us_location_sort_key(result.location, result.workplace_type, result.snippet)[0],
        level_penalty(result.title, result.description or result.snippet),
        0 if age is not None and age <= 24 else 1 if age is None else 2,
        0 if preferred else 1,
        age if age is not None else float("inf"),
    )
