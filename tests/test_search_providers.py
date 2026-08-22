"""Tests for external search adapters with mocked HTTP transports."""

import json

import httpx
import pytest

from agent.search_providers import (
    BraveSearchProvider,
    SearchProviderError,
    SerpApiSearchProvider,
)


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
