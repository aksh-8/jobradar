"""Brave Search and SerpAPI adapters for job discovery."""

from __future__ import annotations

import os
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_SEARCH_TIMEOUT_SECONDS = 30.0


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    snippet: str = ""
    source: str | None = None


class SearchProviderError(RuntimeError):
    """Raised when a configured search service cannot return results."""


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, *, limit: int) -> tuple[SearchResult, ...]: ...


class BraveSearchProvider:
    name = "brave"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = DEFAULT_SEARCH_TIMEOUT_SECONDS,
    ) -> None:
        self.api_key = api_key or os.getenv("BRAVE_SEARCH_API_KEY")
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    async def search(self, query: str, *, limit: int) -> tuple[SearchResult, ...]:
        _require_key(self.api_key, "BRAVE_SEARCH_API_KEY")
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout_seconds
            ) as client:
                response = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    headers={
                        "Accept": "application/json",
                        "X-Subscription-Token": self.api_key,
                    },
                    params={
                        "q": query,
                        "count": min(limit, 20),
                        "country": "us",
                        "search_lang": "en",
                        "safesearch": "moderate",
                    },
                )
                response.raise_for_status()
                rows = response.json().get("web", {}).get("results", [])
            return tuple(
                SearchResult(
                    title=row["title"],
                    url=row["url"],
                    snippet=row.get("description", ""),
                    source=row.get("profile", {}).get("long_name"),
                )
                for row in rows[:limit]
                if row.get("title") and row.get("url")
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
            raise SearchProviderError(f"Brave Search failed: {error}") from error


class SerpApiSearchProvider:
    name = "serpapi"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = DEFAULT_SEARCH_TIMEOUT_SECONDS,
    ) -> None:
        self.api_key = api_key or os.getenv("SERPAPI_API_KEY")
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    async def search(self, query: str, *, limit: int) -> tuple[SearchResult, ...]:
        _require_key(self.api_key, "SERPAPI_API_KEY")
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout_seconds
            ) as client:
                response = await client.get(
                    "https://serpapi.com/search.json",
                    params={
                        "engine": "google",
                        "q": query,
                        "api_key": self.api_key,
                        "num": min(limit, 100),
                        "hl": "en",
                        "gl": "us",
                    },
                )
                response.raise_for_status()
                rows = response.json().get("organic_results", [])
            return tuple(
                SearchResult(
                    title=row["title"],
                    url=row["link"],
                    snippet=row.get("snippet", ""),
                    source=row.get("source") or row.get("displayed_link"),
                )
                for row in rows[:limit]
                if row.get("title") and row.get("link")
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
            raise SearchProviderError(f"SerpAPI search failed: {error}") from error


def create_search_provider(name: str | None = None) -> SearchProvider:
    provider = (name or os.getenv("JOB_SEARCH_PROVIDER", "brave")).casefold()
    if provider == "brave":
        return BraveSearchProvider()
    if provider == "serpapi":
        return SerpApiSearchProvider()
    raise ValueError("JOB_SEARCH_PROVIDER must be 'brave' or 'serpapi'.")


def _require_key(value: str | None, name: str) -> None:
    if not value or value.startswith("replace_with_"):
        raise SearchProviderError(f"{name} is not configured.")
