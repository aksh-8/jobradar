"""Tests for public-web recruiter and hiring-team discovery."""

import pytest

from agent.contact_discovery import ContactType, PublicContactFinder
from agent.search_providers import SearchResult
from backend.red_flag_scanner import JobPostingFacts


class StubSearchProvider:
    name = "stub"

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str, *, limit: int) -> tuple[SearchResult, ...]:
        self.queries.append(query)
        if "technical recruiter" in query:
            return (
                SearchResult(
                    title="Jane Doe - Senior Technical Recruiter at Acme | LinkedIn",
                    url="https://www.linkedin.com/in/jane-doe?trk=search",
                    snippet="Jane recruits engineering talent for Acme.",
                ),
                SearchResult(
                    title="Wrong Person - Technical Recruiter at OtherCo | LinkedIn",
                    url="https://www.linkedin.com/in/wrong-person",
                    snippet="Recruiting at OtherCo.",
                ),
                SearchResult(
                    title="Pat Person - Acme | LinkedIn",
                    url="https://www.linkedin.com/in/pat-person",
                    snippet="Acme is advertising an open technical recruiter position.",
                ),
            )
        if "engineering manager" in query:
            return (
                SearchResult(
                    title="Alex Smith - Engineering Manager, Platform at Acme | LinkedIn",
                    url="https://www.linkedin.com/in/alex-smith/",
                    snippet="Leads the platform engineering team at Acme.",
                ),
            )
        return (
            SearchResult(
                title="Taylor Jones - Senior Platform Engineer at Acme",
                url="https://acme.example/team/taylor-jones",
                snippet="Platform engineering team member at Acme.",
            ),
        )


@pytest.mark.asyncio
async def test_public_contacts_are_ranked_deduplicated_and_canonicalized() -> None:
    provider = StubSearchProvider()
    finder = PublicContactFinder(provider)

    suggestions = await finder.find(
        JobPostingFacts(
            title="Senior Platform Engineer",
            company="Acme",
            description="Build the platform.",
            location="Remote, United States",
        ),
        job_url="https://acme.example/jobs/platform",
        limit=3,
    )

    assert [item.contact_type for item in suggestions] == [
        ContactType.RECRUITER,
        ContactType.HIRING_MANAGER,
        ContactType.TEAM_MEMBER,
    ]
    assert [item.name for item in suggestions] == [
        "Jane Doe",
        "Alex Smith",
        "Taylor Jones",
    ]
    assert suggestions[0].profile_url == "https://www.linkedin.com/in/jane-doe"
    assert all("Verify current employment" in item.evidence for item in suggestions)
    assert len(provider.queries) == 3
    assert all("site:linkedin.com/in" in query for query in provider.queries[:2])
