"""Persistence operations for scored jobs and lifecycle events."""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

from backend.database import database_connection
from backend.red_flag_scanner import JobPostingFacts
from backend.scoring_engine import ScoringResult

TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_NAMES = {"fbclid", "gclid", "trk", "trackingid"}


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
    database_path: str | Path | None = None,
) -> int:
    """Insert or refresh a scored job and return its stable database ID."""

    canonical_url = canonical_job_url(url)
    key = deduplication_key(canonical_url)
    score_details = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    async with database_connection(database_path) as connection:
        cursor = await connection.execute(
            "SELECT id FROM jobs WHERE deduplication_key = ?", (key,)
        )
        existing = await cursor.fetchone()
        await connection.execute(
            """
            INSERT INTO jobs (
                deduplication_key, source, source_job_id, url, title, company,
                description, salary_min_usd, salary_max_usd, company_size,
                sponsorship_status, overall_score, score_details
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(deduplication_key) DO UPDATE SET
                source = excluded.source,
                source_job_id = excluded.source_job_id,
                url = excluded.url,
                title = excluded.title,
                company = excluded.company,
                description = excluded.description,
                salary_min_usd = excluded.salary_min_usd,
                salary_max_usd = excluded.salary_max_usd,
                company_size = excluded.company_size,
                sponsorship_status = excluded.sponsorship_status,
                overall_score = excluded.overall_score,
                score_details = excluded.score_details,
                last_seen_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                key,
                source,
                source_job_id,
                canonical_url,
                posting.title,
                posting.company,
                posting.description,
                posting.base_salary_min_usd,
                posting.base_salary_max_usd,
                posting.company_size,
                posting.sponsorship_status.value,
                result.overall_score,
                score_details,
            ),
        )
        cursor = await connection.execute(
            "SELECT id FROM jobs WHERE deduplication_key = ?", (key,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Saved job could not be retrieved.")
        job_id = int(row["id"])
        if existing is None:
            await connection.execute(
                "INSERT INTO job_events (job_id, event_type) VALUES (?, 'DISCOVERED')",
                (job_id,),
            )
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
