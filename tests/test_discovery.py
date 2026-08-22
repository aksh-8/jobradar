"""Tests for the end-to-end discovery orchestration with local stubs."""

from pathlib import Path

import pytest

from agent.discovery import DiscoveryAgent, configured_queries
from agent.search_providers import SearchResult
from backend.job_store import JobStatus, list_jobs
from backend.red_flag_scanner import JobPostingFacts, SponsorshipStatus
from backend.resume_store import ResumeProfile, ResumeStatus, Skill
from backend.scoring_engine import ProviderAssessment, ScoreDimensions, ScoringEngine


class StubSearch:
    name = "stub-search"

    async def search(self, query: str, *, limit: int):
        return (
            SearchResult(title="Strong", url="https://example.com/strong"),
            SearchResult(title="Rejected", url="https://example.com/rejected"),
            SearchResult(title="Weak", url="https://example.com/weak"),
        )[:limit]


class StubExtractor:
    async def fetch(self, result: SearchResult) -> JobPostingFacts:
        descriptions = {
            "Strong": "Build Python APIs. Visa sponsorship is available.",
            "Rejected": "Build APIs. No visa sponsorship is available.",
            "Weak": "Maintain an unrelated legacy application. Visa sponsorship is available.",
        }
        return JobPostingFacts(
            title=result.title,
            company="Acme",
            description=descriptions[result.title],
            sponsorship_status=(
                SponsorshipStatus.NO
                if result.title == "Rejected"
                else SponsorshipStatus.YES
            ),
            company_size=500,
            base_salary_max_usd=180_000,
        )


class StubAssessmentProvider:
    name = "stub-score"

    def __init__(self) -> None:
        self.calls = 0

    async def score(self, posting, resume) -> ProviderAssessment:
        self.calls += 1
        value = 90 if posting.title == "Strong" else 30
        return ProviderAssessment(
            dimensions=ScoreDimensions(
                role_alignment=value,
                required_skills=value,
                experience_fit=value,
                career_fit=value,
            ),
            matched_requirements=("Python",) if value == 90 else (),
            rationale=("Evidence-based test result.",),
        )


def ready_resume() -> ResumeProfile:
    return ResumeProfile(
        profile_id="ready",
        full_name="Test Candidate",
        status=ResumeStatus.READY,
        summary="Builds Python APIs.",
        target_roles=("Backend Engineer",),
        skills=(Skill(name="Python", category="Languages"),),
    )


@pytest.mark.asyncio
async def test_discovery_filters_persists_and_deduplicates(tmp_path: Path) -> None:
    provider = StubAssessmentProvider()
    database_path = tmp_path / "discovery.db"
    agent = DiscoveryAgent(
        search_provider=StubSearch(),
        extractor=StubExtractor(),
        scoring_engine=ScoringEngine(provider),
        resume=ready_resume(),
        database_path=database_path,
    )

    first = await agent.run(("jobs",), limit=10)
    second = await agent.run(("jobs",), limit=10)

    assert first.searched_results == 3
    assert len(first.opportunities) == 1
    assert first.rejected_results == 1
    assert first.below_threshold_results == 1
    assert provider.calls == 2
    assert second.duplicate_results == 3
    jobs = await list_jobs(database_path=database_path)
    assert len(jobs) == 3
    assert sum(job.status is JobStatus.SKIPPED for job in jobs) == 2


@pytest.mark.asyncio
async def test_discovery_requires_ready_resume(tmp_path: Path) -> None:
    agent = DiscoveryAgent(
        search_provider=StubSearch(),
        extractor=StubExtractor(),
        scoring_engine=ScoringEngine(StubAssessmentProvider()),
        resume=ResumeProfile(profile_id="draft", full_name="Candidate"),
        database_path=tmp_path / "discovery.db",
    )

    with pytest.raises(ValueError, match="must be READY"):
        await agent.run(("jobs",))


def test_configured_queries_accepts_pipe_separated_environment(monkeypatch) -> None:
    monkeypatch.setenv("JOB_SEARCH_QUERIES", "backend jobs | platform jobs")

    assert configured_queries() == ("backend jobs", "platform jobs")
