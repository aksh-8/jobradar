"""Tests for external search adapters with mocked HTTP transports."""

import json

import httpx
import pytest

from agent.search_providers import (
    AshbyBoardProvider,
    BraveSearchProvider,
    GreenhouseBoardProvider,
    SearchProviderError,
    SerpApiGoogleJobsProvider,
    SerpApiSearchProvider,
    _us_location_relevant,
)


@pytest.mark.parametrize(
    "location",
    (
        "Budapest, Hungary",
        "Doha, Qatar",
        "Argentina; Uruguay",
        "Remote - EMEA",
        "Toronto, Canada",
    ),
)
def test_explicit_international_only_locations_are_excluded(location: str) -> None:
    assert _us_location_relevant(location) is False


@pytest.mark.parametrize(
    "location",
    (None, "", "Remote", "United States", "Los Angeles, CA", "New York, NY"),
)
def test_us_or_unknown_locations_remain_eligible(location: str | None) -> None:
    assert _us_location_relevant(location) is True


@pytest.mark.asyncio
async def test_brave_search_authenticates_and_normalizes_results() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Subscription-Token"] == "brave-key"
        assert request.url.params["q"] == "backend jobs"
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Backend Engineer",
                            "url": "https://example.com/job/1",
                            "description": "Build APIs",
                            "profile": {"long_name": "Example"},
                        }
                    ]
                }
            },
        )

    provider = BraveSearchProvider(
        "brave-key", transport=httpx.MockTransport(handler)
    )
    results = await provider.search("backend jobs", limit=5)

    assert len(results) == 1
    assert results[0].title == "Backend Engineer"
    assert results[0].snippet == "Build APIs"


@pytest.mark.asyncio
async def test_serpapi_normalizes_organic_results() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["engine"] == "google"
        assert request.url.params["api_key"] == "serp-key"
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "organic_results": [
                        {
                            "title": "Platform Engineer",
                            "link": "https://example.com/job/2",
                            "snippet": "Cloud platform role",
                            "source": "Example Jobs",
                        }
                    ]
                }
            ).encode(),
        )

    provider = SerpApiSearchProvider(
        "serp-key", transport=httpx.MockTransport(handler)
    )
    results = await provider.search("platform jobs", limit=10)

    assert results[0].url == "https://example.com/job/2"
    assert results[0].source == "Example Jobs"


@pytest.mark.asyncio
async def test_search_provider_requires_real_key() -> None:
    with pytest.raises(SearchProviderError, match="BRAVE_SEARCH_API_KEY"):
        await BraveSearchProvider("replace_with_brave_search_api_key").search(
            "jobs", limit=1
        )


@pytest.mark.asyncio
async def test_google_jobs_paginates_and_prefers_canonical_apply_url() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.params["engine"] == "google_jobs"
        if calls == 1:
            assert "next_page_token" not in request.url.params
            return httpx.Response(
                200,
                json={
                    "jobs_results": [{
                        "title": "Platform Engineer",
                        "company_name": "Acme",
                        "location": "Los Angeles, CA",
                        "job_id": "google-1",
                        "apply_options": [
                            {"title": "LinkedIn", "link": "https://linkedin.com/jobs/view/1"},
                            {"title": "Company", "link": "https://jobs.lever.co/acme/1"},
                        ],
                    }],
                    "serpapi_pagination": {"next_page_token": "page-2"},
                },
            )
        assert request.url.params["next_page_token"] == "page-2"
        return httpx.Response(
            200,
            json={"jobs_results": [{
                "title": "SDET",
                "company_name": "Canopy",
                "location": "Remote, United States",
                "job_id": "google-2",
                "apply_options": [{"title": "Indeed", "link": "https://indeed.com/viewjob?jk=2"}],
            }]},
        )

    provider = SerpApiGoogleJobsProvider(
        "serp-key", transport=httpx.MockTransport(handler)
    )
    results = await provider.search("platform jobs", limit=2)

    assert calls == 2
    assert results[0].url == "https://jobs.lever.co/acme/1"
    assert results[0].source_job_id == "google-1"
    assert results[1].location == "Remote, United States"


@pytest.mark.asyncio
async def test_greenhouse_board_returns_authoritative_posting_facts() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/scaleai/jobs")
        assert request.url.params["content"] == "true"
        return httpx.Response(200, json={"jobs": [
            {
                "id": 1,
                "title": "Enterprise Account Executive",
                "absolute_url": "https://job-boards.greenhouse.io/scaleai/jobs/1",
                "content": "<p>Sell enterprise products.</p>",
                "location": {"name": "New York, NY"},
            },
            {
                "id": 42,
                "title": "Senior Software Engineer",
                "absolute_url": "https://job-boards.greenhouse.io/scaleai/jobs/42",
                "content": "<p>Build automation systems.</p>",
                "location": {"name": "Los Angeles, CA"},
                "updated_at": "2026-08-21T12:00:00Z",
            },
            {
                "id": 43,
                "title": "Forward Deployed Engineer",
                "absolute_url": "https://job-boards.greenhouse.io/scaleai/jobs/43",
                "content": "<p>Build customer systems.</p>",
                "location": {"name": "London, United Kingdom"},
            },
        ]})

    provider = GreenhouseBoardProvider(
        {"scaleai": "Scale AI"}, transport=httpx.MockTransport(handler)
    )
    results = await provider.search("scaleai", limit=10)
    result = results[0]

    assert len(results) == 1
    assert result.ats_name == "greenhouse"
    assert result.source_job_id == "42"
    assert result.description == "Build automation systems."
    assert result.canonical_content is True


@pytest.mark.asyncio
async def test_ashby_board_extracts_annual_usd_compensation() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": [{
            "title": "Forward Deployed Engineer",
            "jobUrl": "https://jobs.ashbyhq.com/glean/job-7",
            "descriptionPlain": "Build customer integrations.",
            "location": "United States",
            "isListed": True,
            "publishedAt": "2026-08-20T00:00:00Z",
            "compensation": {"summaryComponents": [{
                "compensationType": "Salary",
                "currencyCode": "USD",
                "interval": "1 YEAR",
                "minValue": 170000,
                "maxValue": 220000,
            }]},
        }]})

    provider = AshbyBoardProvider(
        {"glean": "Glean"}, transport=httpx.MockTransport(handler)
    )
    result = (await provider.search("glean", limit=10))[0]

    assert result.base_salary_min_usd == 170_000
    assert result.base_salary_max_usd == 220_000
