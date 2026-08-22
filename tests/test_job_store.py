"""Tests for scored-job persistence and lifecycle tracking."""

from pathlib import Path

import pytest

from backend.database import database_connection
from backend.job_store import (
    JobStatus,
    canonical_job_url,
    list_jobs,
    save_scored_job,
    update_job_status,
)
from backend.red_flag_scanner import JobPostingFacts, SponsorshipStatus
from backend.scoring_engine import ScoreDimensions, ScoreVerdict, ScoringResult


def posting() -> JobPostingFacts:
    return JobPostingFacts(
        title="Backend Engineer",
        company="Acme",
        description="Build Python APIs.",
        sponsorship_status=SponsorshipStatus.YES,
        company_size=500,
        base_salary_max_usd=180_000,
    )


def result(score: int = 82) -> ScoringResult:
    return ScoringResult(
        overall_score=score,
        verdict=ScoreVerdict.QUALIFIED,
        provider="test",
        dimensions=ScoreDimensions(
            role_alignment=score,
            required_skills=score,
            experience_fit=score,
            career_fit=score,
        ),
        rationale=("Strong verified match.",),
    )


def test_canonical_url_removes_fragment_and_tracking_parameters() -> None:
    assert canonical_job_url(
        "HTTPS://Example.COM/jobs/42/?utm_source=email&ref=board#apply"
    ) == "https://example.com/jobs/42?ref=board"


@pytest.mark.asyncio
async def test_save_scored_job_deduplicates_and_refreshes_score(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    first_id = await save_scored_job(
        posting(),
        result(82),
        source="extension",
        url="https://example.com/jobs/42?utm_source=email",
        database_path=path,
    )
    second_id = await save_scored_job(
        posting(),
        result(90),
        source="extension",
        url="https://example.com/jobs/42#apply",
        database_path=path,
    )

    jobs = await list_jobs(database_path=path)
    assert first_id == second_id
    assert len(jobs) == 1
    assert jobs[0].overall_score == 90

    async with database_connection(path) as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*) AS count FROM job_events WHERE job_id = ?", (first_id,)
        )
        assert (await cursor.fetchone())["count"] == 1


@pytest.mark.asyncio
async def test_lifecycle_update_records_event_and_skip_reason(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    job_id = await save_scored_job(
        posting(), result(), source="extension", url="https://example.com/jobs/42",
        database_path=path,
    )

    updated = await update_job_status(
        job_id,
        JobStatus.SKIPPED,
        skip_reason="Location mismatch",
        database_path=path,
    )

    assert updated is not None
    assert updated.status is JobStatus.SKIPPED
    assert updated.skip_reason == "Location mismatch"
    assert await list_jobs(status=JobStatus.APPLIED, database_path=path) == ()


@pytest.mark.asyncio
async def test_skipped_status_requires_reason(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="skip_reason"):
        await update_job_status(
            1, JobStatus.SKIPPED, skip_reason=" ", database_path=tmp_path / "jobs.db"
        )
