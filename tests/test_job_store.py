"""Tests for scored-job persistence and lifecycle tracking."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.database import database_connection
from backend.job_store import (
    JobStatus,
    canonical_job_url,
    discovery_track_is_due,
    find_existing_job,
    list_due_follow_ups,
    list_jobs,
    list_pending_digest_jobs,
    mark_digest_jobs_sent,
    mark_follow_ups_sent,
    record_discovery_track_run,
    save_scored_job,
    update_job_status,
    weekly_missing_skills,
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
            skills_match=score,
            experience_level=score,
            domain_relevance=score,
            role_type=score,
            compensation_signal=score,
        ),
        missing_requirements=("Kubernetes",),
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


@pytest.mark.asyncio
async def test_pending_digest_jobs_are_marked_only_after_delivery(
    tmp_path: Path,
) -> None:
    path = tmp_path / "jobs.db"
    job_id = await save_scored_job(
        posting(),
        result(),
        source="brave",
        url="https://example.com/jobs/digest",
        database_path=path,
    )

    pending = await list_pending_digest_jobs(database_path=path)
    assert [job.id for job in pending] == [job_id]
    assert pending[0].posting.company == "Acme"

    await mark_digest_jobs_sent((job_id,), database_path=path)

    assert await list_pending_digest_jobs(database_path=path) == ()


@pytest.mark.asyncio
async def test_applied_job_becomes_due_for_follow_up(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    job_id = await save_scored_job(
        posting(),
        result(),
        source="brave",
        url="https://example.com/jobs/follow-up",
        database_path=path,
    )
    await update_job_status(job_id, JobStatus.APPLIED, database_path=path)
    async with database_connection(path) as connection:
        await connection.execute(
            """
            UPDATE jobs
            SET applied_at = datetime('now', '-15 days'),
                follow_up_due_date = datetime('now', '-1 day')
            WHERE id = ?
            """,
            (job_id,),
        )
        await connection.commit()

    reminders = await list_due_follow_ups(database_path=path)
    assert [reminder.job_id for reminder in reminders] == [job_id]

    await mark_follow_ups_sent((job_id,), database_path=path)
    assert await list_due_follow_ups(database_path=path) == ()


@pytest.mark.asyncio
async def test_weekly_missing_skills_are_aggregated(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    await save_scored_job(
        posting(),
        result(),
        source="brave",
        url="https://example.com/jobs/gaps",
        database_path=path,
    )

    assert await weekly_missing_skills(database_path=path) == (("Kubernetes", 1),)


@pytest.mark.asyncio
async def test_legacy_score_dimensions_remain_readable(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    job_id = await save_scored_job(
        posting(),
        result(),
        source="brave",
        url="https://example.com/jobs/legacy",
        database_path=path,
    )
    legacy = {
        "overall_score": 82,
        "verdict": "QUALIFIED",
        "dimensions": {
            "role_alignment": 82,
            "required_skills": 82,
            "experience_fit": 82,
            "career_fit": 82,
        },
        "missing_requirements": ["Kubernetes"],
        "rationale": ["Legacy score."],
    }
    async with database_connection(path) as connection:
        await connection.execute(
            "UPDATE jobs SET score_details = ? WHERE id = ?",
            (json.dumps(legacy), job_id),
        )
        await connection.commit()

    pending = await list_pending_digest_jobs(database_path=path)

    assert pending[0].score.dimensions.skills_match == 82
    assert await weekly_missing_skills(database_path=path) == (("Kubernetes", 1),)


@pytest.mark.asyncio
async def test_cross_source_identity_prefers_ats_then_natural_key(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    located = posting().model_copy(update={"location": "Los Angeles, CA"})
    job_id = await save_scored_job(
        located,
        result(),
        source="serpapi_google_jobs",
        url="https://indeed.com/viewjob?jk=abc",
        source_job_id="google-1",
        discovery_track="google_jobs",
        database_path=path,
    )

    match = await find_existing_job(
        located,
        url="https://jobs.lever.co/acme/ats-7",
        ats_name="lever",
        source_job_id="ats-7",
        database_path=path,
    )

    assert match is not None
    assert match.job_id == job_id
    assert match.matched_by == "company_title_location"


@pytest.mark.asyncio
async def test_fuzzy_fingerprint_matches_small_description_edits(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    await save_scored_job(
        posting(), result(), source="brave", url="https://example.com/jobs/a",
        database_path=path,
    )
    edited = posting().model_copy(
        update={"description": "Build reliable Python APIs for customers."}
    )

    match = await find_existing_job(
        edited, url="https://aggregator.example/jobs/b", database_path=path
    )

    assert match is not None
    assert match.matched_by == "fuzzy_fingerprint"
    assert match.changed is True


@pytest.mark.asyncio
async def test_discovery_track_due_state_uses_last_success(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)

    assert await discovery_track_is_due(
        "google_jobs", 4, now=now, database_path=path
    )
    await record_discovery_track_run(
        "google_jobs", "RUNNING", now=now, database_path=path
    )
    await record_discovery_track_run(
        "google_jobs", "SUCCEEDED", now=now, database_path=path
    )

    assert not await discovery_track_is_due(
        "google_jobs", 4, now=now + timedelta(hours=3), database_path=path
    )
    assert await discovery_track_is_due(
        "google_jobs", 4, now=now + timedelta(hours=4), database_path=path
    )
