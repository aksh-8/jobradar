"""Persistence operations for scored jobs and lifecycle events."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from enum import Enum
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

from backend.database import database_connection
from backend.red_flag_scanner import JobPostingFacts, SponsorshipStatus
from backend.scoring_engine import ScoringResult

TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_NAMES = {"fbclid", "gclid", "trk", "trackingid"}
AUTOMATIC_DISCOVERY_SOURCES = {
    "brave",
    "serpapi",
    "serpapi_google_jobs",
    "greenhouse",
    "lever",
    "ashby",
    "company_career",
}
SOURCE_PREFERENCE = {
    "greenhouse": 0,
    "lever": 0,
    "ashby": 0,
    "company_career": 1,
    "extension": 1,
    "serpapi_google_jobs": 2,
    "serpapi": 3,
    "brave": 4,
}


class JobStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    VIEWED = "VIEWED"
    SKIPPED = "SKIPPED"
    APPLIED = "APPLIED"


class StoredJob(BaseModel):
    """Dashboard-safe representation of one persisted job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    source: str
    url: str
    title: str
    company: str
    overall_score: int | None
    score_details: dict[str, object] | None
    status: JobStatus
    skip_reason: str | None
    first_discovered_at: str
    last_seen_at: str
    updated_at: str


class PendingDigestJob(BaseModel):
    """A qualified discovery result waiting for successful email delivery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    url: str
    posting: JobPostingFacts
    score: ScoringResult


class FollowUpReminder(BaseModel):
    """An application whose fourteen-day response window has elapsed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: int
    company: str
    title: str
    url: str
    applied_at: str
    follow_up_due_date: str


@dataclass(frozen=True)
class ExistingJobMatch:
    """A layered identity match and whether its substantive facts changed."""

    job_id: int
    changed: bool
    matched_by: str


def canonical_job_url(url: str) -> str:
    """Remove fragments and common tracking parameters before deduplication."""

    parsed = urlsplit(url.strip())
    retained = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in TRACKING_QUERY_NAMES
        and not key.casefold().startswith(TRACKING_QUERY_PREFIXES)
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc.casefold(), path, urlencode(retained), "")
    )


def deduplication_key(url: str) -> str:
    return hashlib.sha256(canonical_job_url(url).encode("utf-8")).hexdigest()


def normalized_job_identity(posting: JobPostingFacts) -> str | None:
    """Build the company/title/location identity used after ATS and URL IDs."""

    if not posting.location:
        return None
    value = "|".join(
        (
            _normalized_identity_text(posting.company),
            _normalized_identity_text(posting.title),
            _normalized_identity_text(posting.location),
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def job_content_hash(posting: JobPostingFacts) -> str:
    """Identify substantive posting changes without depending on source markup."""

    payload = posting.model_dump(mode="json", exclude_none=True)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def fuzzy_content_fingerprint(posting: JobPostingFacts) -> str:
    """Return a 64-bit SimHash resilient to small description edits."""

    tokens = _normalized_identity_text(
        f"{posting.title} {posting.company} {posting.location or ''} {posting.description}"
    ).split()
    features = tokens + [
        " ".join(tokens[index:index + 2]) for index in range(len(tokens) - 1)
    ]
    vector = [0] * 64
    for feature in features:
        value = int.from_bytes(
            hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest(), "big"
        )
        for bit in range(64):
            vector[bit] += 1 if value & (1 << bit) else -1
    fingerprint = sum(1 << bit for bit, weight in enumerate(vector) if weight >= 0)
    return f"{fingerprint:016x}"


async def find_existing_job(
    posting: JobPostingFacts,
    *,
    url: str,
    ats_name: str | None = None,
    source_job_id: str | None = None,
    database_path: str | Path | None = None,
) -> ExistingJobMatch | None:
    """Match by ATS ID, canonical URL, natural identity, then fuzzy content."""

    canonical_url = canonical_job_url(url)
    url_key = deduplication_key(canonical_url)
    natural_key = normalized_job_identity(posting)
    incoming_hash = job_content_hash(posting)
    incoming_fingerprint = fuzzy_content_fingerprint(posting)
    async with database_connection(database_path) as connection:
        row = None
        matched_by = ""
        if ats_name and source_job_id:
            cursor = await connection.execute(
                "SELECT id, content_hash FROM jobs WHERE ats_name = ? AND source_job_id = ?",
                (ats_name.casefold(), source_job_id),
            )
            row = await cursor.fetchone()
            matched_by = "ats_id"
        if row is None:
            cursor = await connection.execute(
                "SELECT id, content_hash FROM jobs WHERE deduplication_key = ?",
                (url_key,),
            )
            row = await cursor.fetchone()
            matched_by = "canonical_url"
        if row is None and natural_key:
            cursor = await connection.execute(
                "SELECT id, content_hash FROM jobs WHERE natural_key = ? ORDER BY id LIMIT 1",
                (natural_key,),
            )
            row = await cursor.fetchone()
            matched_by = "company_title_location"
        if row is None:
            cursor = await connection.execute(
                """
                SELECT id, title, location, content_fingerprint, content_hash
                FROM jobs
                WHERE lower(company) = lower(?) AND content_fingerprint IS NOT NULL
                ORDER BY last_seen_at DESC LIMIT 100
                """,
                (posting.company,),
            )
            for candidate in await cursor.fetchall():
                title_similarity = SequenceMatcher(
                    None,
                    _normalized_identity_text(posting.title),
                    _normalized_identity_text(candidate["title"]),
                ).ratio()
                location_compatible = (
                    not posting.location
                    or not candidate["location"]
                    or _normalized_identity_text(posting.location)
                    == _normalized_identity_text(candidate["location"])
                )
                if (
                    title_similarity >= 0.92
                    and location_compatible
                    and _hamming_distance(
                        incoming_fingerprint, candidate["content_fingerprint"]
                    ) <= 18
                ):
                    row = candidate
                    matched_by = "fuzzy_fingerprint"
                    break
    if row is None:
        return None
    return ExistingJobMatch(
        job_id=int(row["id"]),
        changed=row["content_hash"] != incoming_hash,
        matched_by=matched_by,
    )


async def touch_job_seen(
    job_id: int, *, database_path: str | Path | None = None
) -> None:
    async with database_connection(database_path) as connection:
        await connection.execute(
            "UPDATE jobs SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?", (job_id,)
        )
        await connection.commit()


async def discovery_track_is_due(
    track_name: str,
    interval_hours: int,
    *,
    now: datetime | None = None,
    database_path: str | Path | None = None,
) -> bool:
    """Return whether a track has never succeeded or its interval elapsed."""

    if interval_hours < 1:
        raise ValueError("interval_hours must be at least 1.")
    current = now or datetime.now(timezone.utc)
    async with database_connection(database_path) as connection:
        cursor = await connection.execute(
            "SELECT last_completed_at, status FROM discovery_track_runs WHERE track_name = ?",
            (track_name,),
        )
        row = await cursor.fetchone()
    if row is None or row["status"] != "SUCCEEDED" or not row["last_completed_at"]:
        return True
    completed = datetime.fromisoformat(str(row["last_completed_at"]).replace("Z", "+00:00"))
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)
    return current >= completed + timedelta(hours=interval_hours)


async def record_discovery_track_run(
    track_name: str,
    status: str,
    *,
    error: str | None = None,
    now: datetime | None = None,
    database_path: str | Path | None = None,
) -> None:
    """Record durable track scheduling state after each attempted run."""

    normalized_status = status.upper()
    if normalized_status not in {"RUNNING", "SUCCEEDED", "FAILED"}:
        raise ValueError("status must be RUNNING, SUCCEEDED, or FAILED.")
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    completed = timestamp if normalized_status in {"SUCCEEDED", "FAILED"} else None
    async with database_connection(database_path) as connection:
        await connection.execute(
            """
            INSERT INTO discovery_track_runs (
                track_name, last_started_at, last_completed_at, status, error
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(track_name) DO UPDATE SET
                last_started_at = CASE
                    WHEN excluded.status = 'RUNNING' THEN excluded.last_started_at
                    ELSE discovery_track_runs.last_started_at
                END,
                last_completed_at = excluded.last_completed_at,
                status = excluded.status,
                error = excluded.error
            """,
            (track_name, timestamp, completed, normalized_status, error),
        )
        await connection.commit()


async def job_exists(
    url: str, *, database_path: str | Path | None = None
) -> bool:
    """Return whether a canonical URL has already been discovered."""

    key = deduplication_key(url)
    async with database_connection(database_path) as connection:
        cursor = await connection.execute(
            "SELECT 1 FROM jobs WHERE deduplication_key = ?", (key,)
        )
        return await cursor.fetchone() is not None


async def save_scored_job(
    posting: JobPostingFacts,
    result: ScoringResult,
    *,
    source: str,
    url: str,
    source_job_id: str | None = None,
    ats_name: str | None = None,
    discovery_track: str | None = None,
    database_path: str | Path | None = None,
) -> int:
    """Insert or refresh a scored job and return its stable database ID."""

    canonical_url = canonical_job_url(url)
    key = deduplication_key(canonical_url)
    natural_key = normalized_job_identity(posting)
    fingerprint = fuzzy_content_fingerprint(posting)
    content_hash = job_content_hash(posting)
    normalized_ats = ats_name.casefold() if ats_name else None
    score_details = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    match = await find_existing_job(
        posting,
        url=canonical_url,
        ats_name=normalized_ats,
        source_job_id=source_job_id,
        database_path=database_path,
    )
    async with database_connection(database_path) as connection:
        if match is None:
            cursor = await connection.execute(
                """
                INSERT INTO jobs (
                    deduplication_key, source, source_job_id, url, canonical_url,
                    ats_name, natural_key, content_fingerprint, content_hash,
                    discovery_track, title, company, location, description,
                    employment_type, workplace_type, date_posted, valid_through,
                    salary_min_usd, salary_max_usd, company_size,
                    sponsorship_status, overall_score, score_details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?)
                """,
                (
                    key, source, source_job_id, canonical_url, canonical_url,
                    normalized_ats, natural_key, fingerprint, content_hash,
                    discovery_track, posting.title, posting.company, posting.location,
                    posting.description, posting.employment_type, posting.workplace_type,
                    posting.date_posted, posting.valid_through,
                    posting.base_salary_min_usd, posting.base_salary_max_usd,
                    posting.company_size, posting.sponsorship_status.value,
                    result.overall_score, score_details,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("Saved job could not be retrieved.")
            job_id = int(cursor.lastrowid)
            await connection.execute(
                "INSERT INTO job_events (job_id, event_type) VALUES (?, 'DISCOVERED')",
                (job_id,),
            )
        else:
            cursor = await connection.execute(
                "SELECT source, url, source_job_id, ats_name FROM jobs WHERE id = ?",
                (match.job_id,),
            )
            existing = await cursor.fetchone()
            if existing is None:
                raise RuntimeError("Matched job disappeared before update.")
            prefer_incoming = SOURCE_PREFERENCE.get(source, 50) < SOURCE_PREFERENCE.get(
                existing["source"], 50
            )
            stored_source = source if prefer_incoming else existing["source"]
            stored_url = canonical_url if prefer_incoming else existing["url"]
            stored_source_job_id = (
                source_job_id if prefer_incoming or not existing["source_job_id"]
                else existing["source_job_id"]
            )
            stored_ats = (
                normalized_ats if prefer_incoming or not existing["ats_name"]
                else existing["ats_name"]
            )
            await connection.execute(
                """
                UPDATE jobs SET
                    source = ?, source_job_id = ?, ats_name = ?, url = ?,
                    canonical_url = ?, natural_key = ?, content_fingerprint = ?,
                    content_hash = ?, discovery_track = COALESCE(?, discovery_track),
                    title = ?, company = ?, location = ?, description = ?,
                    employment_type = ?, workplace_type = ?, date_posted = ?,
                    valid_through = ?, salary_min_usd = ?, salary_max_usd = ?,
                    company_size = ?, sponsorship_status = ?, overall_score = ?,
                    score_details = ?, last_seen_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    stored_source, stored_source_job_id, stored_ats, stored_url,
                    stored_url, natural_key, fingerprint, content_hash, discovery_track,
                    posting.title, posting.company, posting.location, posting.description,
                    posting.employment_type, posting.workplace_type, posting.date_posted,
                    posting.valid_through, posting.base_salary_min_usd,
                    posting.base_salary_max_usd, posting.company_size,
                    posting.sponsorship_status.value, result.overall_score,
                    score_details, match.job_id,
                ),
            )
            job_id = match.job_id
        await connection.commit()
        return job_id


async def list_jobs(
    *,
    status: JobStatus | None = None,
    database_path: str | Path | None = None,
) -> tuple[StoredJob, ...]:
    query = """
        SELECT id, source, url, title, company, overall_score, score_details,
               status, skip_reason, first_discovered_at, last_seen_at, updated_at
        FROM jobs
    """
    parameters: tuple[str, ...] = ()
    if status is not None:
        query += " WHERE status = ?"
        parameters = (status.value,)
    query += " ORDER BY overall_score DESC, last_seen_at DESC, id DESC"

    async with database_connection(database_path) as connection:
        cursor = await connection.execute(query, parameters)
        rows = await cursor.fetchall()
    return tuple(_stored_job(row) for row in rows)


async def list_pending_digest_jobs(
    *,
    minimum_score: int = 60,
    database_path: str | Path | None = None,
) -> tuple[PendingDigestJob, ...]:
    """Return qualified discovery jobs not yet included in a delivered digest."""

    async with database_connection(database_path) as connection:
        cursor = await connection.execute(
            """
            SELECT id, url, title, company, description, salary_min_usd,
                   salary_max_usd, company_size, sponsorship_status, score_details,
                   location, employment_type, workplace_type, date_posted, valid_through
            FROM jobs
            WHERE digest_sent_at IS NULL
              AND source IN (
                  'brave', 'serpapi', 'serpapi_google_jobs', 'greenhouse',
                  'lever', 'ashby', 'company_career'
              )
              AND status != 'SKIPPED'
              AND overall_score >= ?
            ORDER BY overall_score DESC, first_discovered_at, id
            """,
            (minimum_score,),
        )
        rows = await cursor.fetchall()

    pending = []
    for row in rows:
        if not row["description"] or not row["score_details"]:
            continue
        pending.append(
            PendingDigestJob(
                id=row["id"],
                url=row["url"],
                posting=JobPostingFacts(
                    title=row["title"],
                    company=row["company"],
                    description=row["description"],
                    location=row["location"],
                    employment_type=row["employment_type"],
                    workplace_type=row["workplace_type"],
                    date_posted=row["date_posted"],
                    valid_through=row["valid_through"],
                    sponsorship_status=SponsorshipStatus(row["sponsorship_status"]),
                    company_size=row["company_size"],
                    base_salary_min_usd=row["salary_min_usd"],
                    base_salary_max_usd=row["salary_max_usd"],
                ),
                score=_scoring_result(row["score_details"]),
            )
        )
    return tuple(pending)


async def mark_digest_jobs_sent(
    job_ids: tuple[int, ...],
    *,
    database_path: str | Path | None = None,
) -> None:
    """Mark jobs delivered only after the email provider succeeds."""

    if not job_ids:
        return
    placeholders = ",".join("?" for _ in job_ids)
    async with database_connection(database_path) as connection:
        await connection.execute(
            f"UPDATE jobs SET digest_sent_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
            job_ids,
        )
        await connection.commit()


async def list_due_follow_ups(
    *, database_path: str | Path | None = None
) -> tuple[FollowUpReminder, ...]:
    """Return applied roles with no response after the fourteen-day window."""

    async with database_connection(database_path) as connection:
        cursor = await connection.execute(
            """
            SELECT id, company, title, url, applied_at, follow_up_due_date
            FROM jobs
            WHERE status = 'APPLIED'
              AND response_received = 0
              AND follow_up_sent = 0
              AND follow_up_due_date IS NOT NULL
              AND datetime(follow_up_due_date) <= datetime('now')
            ORDER BY follow_up_due_date, id
            """
        )
        rows = await cursor.fetchall()
    return tuple(
        FollowUpReminder(
            job_id=row["id"],
            company=row["company"],
            title=row["title"],
            url=row["url"],
            applied_at=row["applied_at"],
            follow_up_due_date=row["follow_up_due_date"],
        )
        for row in rows
    )


async def mark_follow_ups_sent(
    job_ids: tuple[int, ...],
    *,
    database_path: str | Path | None = None,
) -> None:
    if not job_ids:
        return
    placeholders = ",".join("?" for _ in job_ids)
    async with database_connection(database_path) as connection:
        await connection.execute(
            f"UPDATE jobs SET follow_up_sent = 1 WHERE id IN ({placeholders})",
            job_ids,
        )
        await connection.commit()


async def weekly_missing_skills(
    *,
    limit: int = 5,
    database_path: str | Path | None = None,
) -> tuple[tuple[str, int], ...]:
    """Aggregate recurring missing requirements from the previous seven days."""

    async with database_connection(database_path) as connection:
        cursor = await connection.execute(
            """
            SELECT score_details
            FROM jobs
            WHERE score_details IS NOT NULL
              AND status != 'SKIPPED'
              AND datetime(first_discovered_at) >= datetime('now', '-7 days')
            """
        )
        rows = await cursor.fetchall()
    counts: dict[str, tuple[str, int]] = {}
    for row in rows:
        result = _scoring_result(row["score_details"])
        for skill in result.missing_requirements:
            key = skill.casefold().strip()
            if not key:
                continue
            display, count = counts.get(key, (skill, 0))
            counts[key] = (display, count + 1)
    ordered = sorted(counts.values(), key=lambda item: (-item[1], item[0].casefold()))
    return tuple(ordered[:limit])


async def update_job_status(
    job_id: int,
    new_status: JobStatus,
    *,
    skip_reason: str | None = None,
    database_path: str | Path | None = None,
) -> StoredJob | None:
    """Update lifecycle state and append its matching immutable event."""

    reason = skip_reason.strip() if skip_reason else None
    if new_status is JobStatus.SKIPPED and not reason:
        raise ValueError("skip_reason is required when status is SKIPPED.")
    if new_status is not JobStatus.SKIPPED:
        reason = None

    async with database_connection(database_path) as connection:
        cursor = await connection.execute("SELECT id FROM jobs WHERE id = ?", (job_id,))
        if await cursor.fetchone() is None:
            return None
        if new_status is JobStatus.APPLIED:
            await connection.execute(
                """
                UPDATE jobs
                SET status = ?, skip_reason = NULL,
                    applied_at = COALESCE(applied_at, CURRENT_TIMESTAMP),
                    follow_up_due_date = COALESCE(
                        follow_up_due_date,
                        datetime(CURRENT_TIMESTAMP, '+14 days')
                    ),
                    response_received = 0,
                    follow_up_sent = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (new_status.value, job_id),
            )
        else:
            await connection.execute(
                """
                UPDATE jobs
                SET status = ?, skip_reason = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (new_status.value, reason, job_id),
            )
        await connection.execute(
            "INSERT INTO job_events (job_id, event_type, details) VALUES (?, ?, ?)",
            (job_id, new_status.value, reason),
        )
        await connection.commit()
        cursor = await connection.execute(
            """
            SELECT id, source, url, title, company, overall_score, score_details,
                   status, skip_reason, first_discovered_at, last_seen_at, updated_at
            FROM jobs WHERE id = ?
            """,
            (job_id,),
        )
        row = await cursor.fetchone()
    return _stored_job(row) if row is not None else None


def _stored_job(row) -> StoredJob:
    raw_details = row["score_details"]
    return StoredJob(
        id=row["id"],
        source=row["source"],
        url=row["url"],
        title=row["title"],
        company=row["company"],
        overall_score=row["overall_score"],
        score_details=json.loads(raw_details) if raw_details else None,
        status=JobStatus(row["status"]),
        skip_reason=row["skip_reason"],
        first_discovered_at=row["first_discovered_at"],
        last_seen_at=row["last_seen_at"],
        updated_at=row["updated_at"],
    )


def _scoring_result(raw: str) -> ScoringResult:
    """Load current score JSON and upgrade the pre-Step-11 dimension shape."""

    data = json.loads(raw)
    dimensions = data.get("dimensions")
    if isinstance(dimensions, dict) and "role_alignment" in dimensions:
        data["dimensions"] = {
            "skills_match": dimensions.get("required_skills", 0),
            "experience_level": dimensions.get("experience_fit", 0),
            "domain_relevance": dimensions.get("role_alignment", 0),
            "role_type": dimensions.get("career_fit", 0),
            "compensation_signal": dimensions.get("role_alignment", 0),
        }
    return ScoringResult.model_validate(data)


def _normalized_identity_text(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def _hamming_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()
