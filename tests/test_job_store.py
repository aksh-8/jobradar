"""Tests for scored-job persistence and lifecycle tracking."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.database import database_connection
from backend.job_store import (
    JobStatus,
    canonical_job_url,
    delete_stored_job,
    discovery_track_is_due,
    find_existing_job,
    get_scored_job_details,
    list_due_follow_ups,
    list_jobs,
    list_pending_digest_jobs,
    mark_digest_jobs_sent,
    mark_follow_ups_sent,
    mark_job_closed_by_url,
    record_discovery_track_run,
    save_scored_job,
    update_job_status,
    weekly_missing_skills,
)
from backend.red_flag_scanner import (
    FlagSeverity,
    JobPostingFacts,
    RedFlag,
    RedFlagCode,
    SponsorshipStatus,
)
from backend.scoring_engine import ScoreDimensions, ScoreVerdict, ScoringResult


def posting() -> JobPostingFacts:
    return JobPostingFacts(
        title="Backend Engineer",
        company="Acme",
        description="Build Python APIs.",
        location="Los Angeles, CA",
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


def hard_rejected_result() -> ScoringResult:
    return ScoringResult(
        overall_score=0,
        verdict=ScoreVerdict.REJECTED,
        hard_flags=(
            RedFlag(
                code=RedFlagCode.NO_SPONSORSHIP,
                severity=FlagSeverity.HARD_FILTER,
                message="No sponsorship.",
                evidence="No visa sponsorship.",
            ),
        ),
        rationale=("Rejected by deterministic hard-filter policy.",),
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
async def test_us_only_jobs_hide_foreign_roles_and_follow_location_priority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "locations.db"
    locations = (
        ("Austin", "Austin, TX"),
        ("New York", "New York, NY"),
        ("San Francisco", "San Francisco, CA"),
        ("Remote", "Remote, United States"),
        ("Los Angeles", "Los Angeles, CA"),
        ("India", "Bengaluru, India"),
    )
    for title, location in locations:
        await save_scored_job(
            posting().model_copy(update={"title": title, "location": location}),
            result(),
            source="brave",
            url=f"https://example.com/jobs/{title.casefold().replace(' ', '-')}",
            database_path=path,
        )

    jobs = await list_jobs(us_only=True, database_path=path)

    assert [job.title for job in jobs] == [
        "Los Angeles",
        "Remote",
        "San Francisco",
        "New York",
        "Austin",
    ]
    assert [job.location_tier for job in jobs] == [
        "Los Angeles priority",
        "Remote — United States",
        "California",
        "East Coast",
        "United States",
    ]
    assert all(job.title != "India" for job in jobs)


@pytest.mark.asyncio
async def test_dashboard_sorts_score_within_each_location_tier(tmp_path: Path) -> None:
    path = tmp_path / "location-scores.db"
    await save_scored_job(
        posting().model_copy(update={"title": "Lower LA", "location": "Burbank, CA"}),
        result(61),
        source="brave",
        url="https://example.com/jobs/lower-la",
        database_path=path,
    )
    await save_scored_job(
        posting().model_copy(update={"title": "Higher LA", "location": "Santa Monica, CA"}),
        result(91),
        source="brave",
        url="https://example.com/jobs/higher-la",
        database_path=path,
    )

    jobs = await list_jobs(us_only=True, database_path=path)

    assert [job.title for job in jobs] == ["Higher LA", "Lower LA"]


@pytest.mark.asyncio
async def test_pending_digest_and_details_exclude_foreign_jobs(tmp_path: Path) -> None:
    path = tmp_path / "us-digest.db"
    us_id = await save_scored_job(
        posting(),
        result(),
        source="brave",
        url="https://example.com/jobs/us",
        database_path=path,
    )
    foreign_id = await save_scored_job(
        posting().model_copy(update={"title": "India role", "location": "India"}),
        result(),
        source="brave",
        url="https://example.com/jobs/india",
        database_path=path,
    )

    assert [job.id for job in await list_pending_digest_jobs(database_path=path)] == [
        us_id
    ]
    assert await get_scored_job_details(us_id, database_path=path) is not None
    assert await get_scored_job_details(foreign_id, database_path=path) is None


@pytest.mark.asyncio
async def test_closed_and_expired_jobs_are_hidden_from_read_surfaces(tmp_path: Path) -> None:
    path = tmp_path / "availability.db"
    closed_id = await save_scored_job(
        posting(),
        result(),
        source="serpapi_google_jobs",
        url="https://jobright.ai/jobs/info/closed-role",
        database_path=path,
    )
    await mark_job_closed_by_url(
        "https://jobright.ai/jobs/info/closed-role?visit=alert",
        "Page reports closure: This job has closed.",
        database_path=path,
    )
    await save_scored_job(
        posting().model_copy(update={"title": "Expired", "valid_through": "2020-01-01"}),
        result(),
        source="brave",
        url="https://example.com/jobs/expired",
        database_path=path,
    )

    assert await list_jobs(us_only=True, database_path=path) == ()
    assert await list_pending_digest_jobs(database_path=path) == ()
    assert await get_scored_job_details(closed_id, database_path=path) is None


@pytest.mark.asyncio
async def test_any_user_selected_job_can_be_deleted(tmp_path: Path) -> None:
    path = tmp_path / "delete.db"
    rejected_id = await save_scored_job(
        posting(),
        hard_rejected_result(),
        source="brave",
        url="https://example.com/jobs/rejected",
        database_path=path,
    )
    await update_job_status(
        rejected_id,
        JobStatus.SKIPPED,
        skip_reason="NO_SPONSORSHIP",
        database_path=path,
    )
    qualified_id = await save_scored_job(
        posting().model_copy(update={"title": "Qualified"}),
        result(),
        source="brave",
        url="https://example.com/jobs/qualified",
        database_path=path,
    )

    assert await delete_stored_job(qualified_id, database_path=path)
    assert await delete_stored_job(rejected_id, database_path=path)
    assert await list_jobs(database_path=path) == ()
    async with database_connection(path) as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*) AS count FROM job_events WHERE job_id = ?",
            (rejected_id,),
        )
        assert (await cursor.fetchone())["count"] == 0


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
    unlocated = posting().model_copy(update={"location": None})
    await save_scored_job(
        unlocated, result(), source="brave", url="https://example.com/jobs/a",
        database_path=path,
    )
    edited = unlocated.model_copy(
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
