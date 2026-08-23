"""Deterministic availability checks for closed and expired job postings."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone


_CLOSED_PATTERNS = (
    re.compile(r"\bthis job has closed\b", re.IGNORECASE),
    re.compile(r"\bthis job is closed\b", re.IGNORECASE),
    re.compile(
        r"\b(?:this )?(?:job|position|posting|requisition) "
        r"(?:is|has been) no longer available\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:this )?(?:job|position|posting|requisition) "
        r"(?:is|has) (?:been )?(?:closed|expired|filled)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bno longer accepting applications\b", re.IGNORECASE),
    re.compile(r"\bapplications (?:are|have been) closed\b", re.IGNORECASE),
)


def closure_reason(
    *,
    valid_through: str | None = None,
    page_text: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """Return auditable closure evidence, or ``None`` when a role appears open."""

    if valid_through and _is_expired(valid_through, now=now):
        return f"Posting expired after {valid_through.strip()}."
    rendered = " ".join((page_text or "").split())
    for pattern in _CLOSED_PATTERNS:
        if match := pattern.search(rendered):
            return f"Page reports closure: {match.group(0)}."
    return None


def is_closed_job(
    *,
    valid_through: str | None = None,
    page_text: str | None = None,
    closed_at: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Return whether stored or current evidence says a posting is unavailable."""

    return bool(closed_at) or closure_reason(
        valid_through=valid_through,
        page_text=page_text,
        now=now,
    ) is not None


def _is_expired(value: str, *, now: datetime | None = None) -> bool:
    rendered = value.strip()
    if not rendered:
        return False
    current = now or datetime.now(timezone.utc)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", rendered):
            return date.fromisoformat(rendered) < current.date()
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= current.astimezone(parsed.tzinfo)
