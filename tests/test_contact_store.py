"""Tests for cached public contact suggestions."""

from pathlib import Path

import pytest

from agent.contact_discovery import ContactSuggestion, ContactType
from backend.contact_store import (
    get_cached_contact_suggestions,
    replace_contact_suggestions,
)
from backend.database import database_connection
from backend.job_store import delete_stored_job, save_scored_job
from backend.red_flag_scanner import JobPostingFacts
from backend.scoring_engine import ScoreDimensions, ScoreVerdict, ScoringResult


async def _saved_job(path: Path) -> int:
    return await save_scored_job(
        JobPostingFacts(
            title="Platform Engineer",
            company="Acme",
            description="Build a platform.",
            location="Los Angeles, CA",
        ),
        ScoringResult(
            overall_score=80,
            verdict=ScoreVerdict.QUALIFIED,
            provider="test",
            dimensions=ScoreDimensions(
                skills_match=80,
                experience_level=80,
                domain_relevance=80,
                role_type=80,
                compensation_signal=80,
            ),
        ),
        source="test",
        url="https://acme.example/jobs/platform",
        database_path=path,
    )


@pytest.mark.asyncio
async def test_contact_cache_preserves_results_and_empty_searches(tmp_path: Path) -> None:
    path = tmp_path / "contacts.db"
    job_id = await _saved_job(path)
    suggestion = ContactSuggestion(
        name="Jane Doe",
        title="Technical Recruiter at Acme",
        contact_type=ContactType.RECRUITER,
        profile_url="https://www.linkedin.com/in/jane-doe",
        source="LinkedIn public search result",
        confidence=94,
        evidence="Verify current employment before outreach.",
    )

    assert await get_cached_contact_suggestions(job_id, database_path=path) is None
    await replace_contact_suggestions(job_id, (suggestion,), database_path=path)
    assert await get_cached_contact_suggestions(job_id, database_path=path) == (
        suggestion,
    )

    await replace_contact_suggestions(job_id, (), database_path=path)
    assert await get_cached_contact_suggestions(job_id, database_path=path) == ()


@pytest.mark.asyncio
async def test_deleting_job_cascades_contact_cache(tmp_path: Path) -> None:
    path = tmp_path / "contacts.db"
    job_id = await _saved_job(path)
    await replace_contact_suggestions(job_id, (), database_path=path)

    assert await delete_stored_job(job_id, database_path=path)
    async with database_connection(path) as connection:
        contacts = await connection.execute(
            "SELECT COUNT(*) AS count FROM job_contacts WHERE job_id = ?", (job_id,)
        )
        searches = await connection.execute(
            "SELECT COUNT(*) AS count FROM job_contact_searches WHERE job_id = ?",
            (job_id,),
        )
        assert (await contacts.fetchone())["count"] == 0
        assert (await searches.fetchone())["count"] == 0
