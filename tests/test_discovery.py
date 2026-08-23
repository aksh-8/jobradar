"""Tests for the end-to-end discovery orchestration with local stubs."""

from pathlib import Path

import pytest

from agent.discovery import (
    DiscoveryAgent,
    ScoringBudget,
    _query_result_budgets,
    configured_queries,
)
from agent.posting_extractor import ClosedJobPostingError
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
            location="Los Angeles, CA",
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
                skills_match=value,
                experience_level=value,
                domain_relevance=value,
                role_type=value,
                compensation_signal=value,
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


@pytest.mark.asyncio
async def test_shared_scoring_budget_defers_only_model_scored_postings(
    tmp_path: Path,
) -> None:
    provider = StubAssessmentProvider()
    agent = DiscoveryAgent(
        search_provider=StubSearch(),
        extractor=StubExtractor(),
        scoring_engine=ScoringEngine(provider),
        resume=ready_resume(),
        scoring_budget=ScoringBudget(1),
        database_path=tmp_path / "budget.db",
    )

    report = await agent.run(("jobs",), limit=10)

    assert provider.calls == 1
    assert report.deferred_results == 1
    assert report.rejected_results == 1


@pytest.mark.asyncio
async def test_discovery_rejects_non_us_postings_before_scoring(tmp_path: Path) -> None:
    class ForeignExtractor(StubExtractor):
        async def fetch(self, result: SearchResult) -> JobPostingFacts:
            posting = await super().fetch(result)
            return posting.model_copy(update={"location": "Bengaluru, India"})

    provider = StubAssessmentProvider()
    agent = DiscoveryAgent(
        search_provider=StubSearch(),
        extractor=ForeignExtractor(),
        scoring_engine=ScoringEngine(provider),
        resume=ready_resume(),
        database_path=tmp_path / "foreign.db",
    )

    report = await agent.run(("jobs",), limit=10)

    assert report.rejected_results == 3
    assert provider.calls == 0
    assert await list_jobs(database_path=tmp_path / "foreign.db") == ()


@pytest.mark.asyncio
async def test_discovery_marks_a_previously_saved_job_closed(tmp_path: Path) -> None:
    class SingleSearch:
        name = "serpapi_google_jobs"

        async def search(self, query: str, *, limit: int):
            return (SearchResult(title="Strong", url="https://example.com/strong"),)

    class ClosedExtractor:
        async def fetch(self, result: SearchResult) -> JobPostingFacts:
            raise ClosedJobPostingError(
                f"{result.url} is closed: Page reports closure: This job has closed."
            )

    path = tmp_path / "closed.db"
    open_agent = DiscoveryAgent(
        search_provider=SingleSearch(),
        extractor=StubExtractor(),
        scoring_engine=ScoringEngine(StubAssessmentProvider()),
        resume=ready_resume(),
        database_path=path,
    )
    await open_agent.run(("jobs",), limit=1)
    assert len(await list_jobs(database_path=path)) == 1

    closed_agent = DiscoveryAgent(
        search_provider=SingleSearch(),
        extractor=ClosedExtractor(),
        scoring_engine=ScoringEngine(StubAssessmentProvider()),
        resume=ready_resume(),
        database_path=path,
    )
    report = await closed_agent.run(("jobs",), limit=1)

    assert report.rejected_results == 1
    assert await list_jobs(database_path=path) == ()


def test_configured_queries_accepts_pipe_separated_environment(monkeypatch) -> None:
    monkeypatch.setenv("JOB_SEARCH_QUERIES", "backend jobs | platform jobs")

    assert configured_queries() == ("backend jobs", "platform jobs")


def test_query_budget_distributes_remainder_without_starving_google_matrix() -> None:
    budgets = _query_result_budgets(25, 60)

    assert len(budgets) == 25
    assert sum(budgets) == 60
    assert budgets[:10] == (3,) * 10
    assert budgets[10:] == (2,) * 15
