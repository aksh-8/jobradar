"""Search and public job-board adapters used by JobRadar discovery tracks."""

from __future__ import annotations

import html
import os
import re
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from backend.location_policy import is_us_based

DEFAULT_SEARCH_TIMEOUT_SECONDS = 30.0
AGGREGATOR_HOSTS = (
    "linkedin.com",
    "indeed.com",
    "ziprecruiter.com",
    "google.com",
    "jobright.ai",
    "bebee.com",
    "talent.com",
    "builtin.com",
    "bandana.com",
    "trabajo.org",
    "jobrapido.com",
)
CANONICAL_JOB_HOST_MARKERS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "jobs.apple.com",
    "careers.google.com",
    "google.com/about/careers",
    "careers.microsoft.com",
    "amazon.jobs",
    "metacareers.com",
    "nvidia.com/en-us/about-nvidia/careers",
    "tesla.com/careers",
)


class SearchResult(BaseModel):
    """One candidate posting plus facts supplied by an authoritative feed."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    snippet: str = ""
    source: str | None = None
    source_job_id: str | None = None
    ats_name: str | None = None
    company: str | None = None
    description: str | None = None
    location: str | None = None
    employment_type: str | None = None
    workplace_type: str | None = None
    base_salary_min_usd: int | None = Field(default=None, ge=0)
    base_salary_max_usd: int | None = Field(default=None, ge=0)
    date_posted: str | None = None
    valid_through: str | None = None
    canonical_content: bool = False


class SearchProviderError(RuntimeError):
    """Raised when a configured discovery source cannot return results."""


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, *, limit: int) -> tuple[SearchResult, ...]: ...


class BraveSearchProvider:
    """Broad web discovery used for aggregator and custom-careers gaps."""

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
        results: list[SearchResult] = []
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout_seconds
            ) as client:
                offset = 0
                while len(results) < limit and offset <= 9:
                    response = await client.get(
                        "https://api.search.brave.com/res/v1/web/search",
                        headers={
                            "Accept": "application/json",
                            "X-Subscription-Token": self.api_key,
                        },
                        params={
                            "q": query,
                            "count": min(limit - len(results), 20),
                            "offset": offset,
                            "country": "us",
                            "search_lang": "en",
                            "safesearch": "moderate",
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                    rows = payload.get("web", {}).get("results", [])
                    results.extend(
                        SearchResult(
                            title=row["title"],
                            url=row["url"],
                            snippet=row.get("description", ""),
                            source=row.get("profile", {}).get("long_name"),
                        )
                        for row in rows
                        if row.get("title") and row.get("url")
                    )
                    if not payload.get("query", {}).get("more_results_available"):
                        break
                    offset += 1
            return tuple(results[:limit])
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
            raise SearchProviderError(f"Brave Search failed: {error}") from error


class SerpApiSearchProvider:
    """Legacy ordinary-Google adapter retained for explicit compatibility."""

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
            results = tuple(
                SearchResult(
                    title=row["title"],
                    url=row["link"],
                    snippet=row.get("snippet", ""),
                    source=row.get("source") or row.get("displayed_link"),
                )
                for row in rows[:limit]
                if row.get("title") and row.get("link")
            )
            return results
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
            raise SearchProviderError(f"SerpAPI search failed: {error}") from error


class SerpApiGoogleJobsProvider:
    """Structured Google Jobs discovery with next-page-token pagination."""

    name = "serpapi_google_jobs"

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
        results: list[SearchResult] = []
        next_page_token: str | None = None
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout_seconds
            ) as client:
                while len(results) < limit:
                    params = {
                        "engine": "google_jobs",
                        "q": query,
                        "api_key": self.api_key,
                        "hl": "en",
                        "gl": "us",
                    }
                    if next_page_token:
                        params["next_page_token"] = next_page_token
                    response = await client.get(
                        "https://serpapi.com/search.json", params=params
                    )
                    response.raise_for_status()
                    payload = response.json()
                    rows = payload.get("jobs_results", [])
                    results.extend(
                        _google_jobs_result(row)
                        for row in rows
                        if row.get("title") and row.get("company_name")
                    )
                    next_page_token = payload.get("serpapi_pagination", {}).get(
                        "next_page_token"
                    )
                    if not rows or not next_page_token:
                        break
            return tuple(results[:limit])
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
            raise SearchProviderError(f"SerpAPI Google Jobs failed: {error}") from error


class PriorityGoogleJobsProvider:
    """Structured Google Jobs lookup isolated to one priority employer."""

    name = "priority_google_jobs"

    def __init__(
        self,
        companies: tuple[str, ...],
        *,
        search_provider: SearchProvider | None = None,
    ) -> None:
        self.companies = companies
        self.search_provider = search_provider or SerpApiGoogleJobsProvider()

    async def search(self, query: str, *, limit: int) -> tuple[SearchResult, ...]:
        company = next(
            (item for item in self.companies if item.casefold() == query.casefold()),
            None,
        )
        if company is None:
            raise SearchProviderError(f"Unknown priority company {query!r}.")
        role_clause = (
            '("software engineer" OR "platform engineer" OR '
            '"infrastructure engineer" OR SDET OR "applied AI engineer" OR '
            '"forward deployed engineer")'
        )
        results = await self.search_provider.search(
            f'{company} {role_clause} jobs in "United States"',
            limit=limit,
        )
        return tuple(
            result
            for result in results
            if result.company and _company_name_matches(company, result.company)
        )


class GreenhouseBoardProvider:
    """Fetch every published job from configured public Greenhouse boards."""

    name = "greenhouse"

    def __init__(self, boards: dict[str, str], **client_options: object) -> None:
        self.boards = boards
        self.transport = client_options.get("transport")
        self.timeout_seconds = float(
            client_options.get("timeout_seconds", DEFAULT_SEARCH_TIMEOUT_SECONDS)
        )

    async def search(self, query: str, *, limit: int) -> tuple[SearchResult, ...]:
        company = _configured_company(self.boards, query, "Greenhouse")
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout_seconds
            ) as client:
                response = await client.get(
                    f"https://boards-api.greenhouse.io/v1/boards/{query}/jobs",
                    params={"content": "true"},
                )
                response.raise_for_status()
                rows = response.json().get("jobs", [])
            results = tuple(
                SearchResult(
                    title=row["title"],
                    url=row["absolute_url"],
                    source=company,
                    source_job_id=str(row["id"]),
                    ats_name="greenhouse",
                    company=company,
                    description=_plain_text(row.get("content", "")),
                    location=(row.get("location") or {}).get("name"),
                    date_posted=row.get("updated_at"),
                    canonical_content=True,
                )
                for row in rows
                if row.get("title")
                and _target_role_title(str(row["title"]))
                and _us_location_relevant((row.get("location") or {}).get("name"))
                and row.get("absolute_url")
                and row.get("id")
            )
            return results[:limit]
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
            raise SearchProviderError(f"Greenhouse board {query!r} failed: {error}") from error


class LeverBoardProvider:
    """Fetch every published job from configured public Lever boards."""

    name = "lever"

    def __init__(self, boards: dict[str, str], **client_options: object) -> None:
        self.boards = boards
        self.transport = client_options.get("transport")
        self.timeout_seconds = float(
            client_options.get("timeout_seconds", DEFAULT_SEARCH_TIMEOUT_SECONDS)
        )

    async def search(self, query: str, *, limit: int) -> tuple[SearchResult, ...]:
        company = _configured_company(self.boards, query, "Lever")
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout_seconds
            ) as client:
                response = await client.get(
                    f"https://api.lever.co/v0/postings/{query}",
                    params={"mode": "json", "limit": limit},
                )
                response.raise_for_status()
                rows = response.json()
            results = tuple(
                SearchResult(
                    title=row["text"],
                    url=row["hostedUrl"],
                    source=company,
                    source_job_id=str(row["id"]),
                    ats_name="lever",
                    company=company,
                    description=_lever_description(row),
                    location=(row.get("categories") or {}).get("location"),
                    employment_type=(row.get("categories") or {}).get("commitment"),
                    workplace_type=row.get("workplaceType"),
                    date_posted=(str(row["createdAt"]) if row.get("createdAt") else None),
                    canonical_content=True,
                )
                for row in rows
                if row.get("text")
                and _target_role_title(str(row["text"]))
                and _us_location_relevant((row.get("categories") or {}).get("location"))
                and row.get("hostedUrl")
                and row.get("id")
            )
            return results[:limit]
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
            raise SearchProviderError(f"Lever board {query!r} failed: {error}") from error


class AshbyBoardProvider:
    """Fetch published roles and compensation from configured Ashby boards."""

    name = "ashby"

    def __init__(self, boards: dict[str, str], **client_options: object) -> None:
        self.boards = boards
        self.transport = client_options.get("transport")
        self.timeout_seconds = float(
            client_options.get("timeout_seconds", DEFAULT_SEARCH_TIMEOUT_SECONDS)
        )

    async def search(self, query: str, *, limit: int) -> tuple[SearchResult, ...]:
        company = _configured_company(self.boards, query, "Ashby")
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout_seconds
            ) as client:
                response = await client.get(
                    f"https://api.ashbyhq.com/posting-api/job-board/{query}",
                    params={"includeCompensation": "true"},
                )
                response.raise_for_status()
                rows = response.json().get("jobs", [])
            results = tuple(
                _ashby_result(row, company)
                for row in rows
                if row.get("isListed", True)
                and row.get("title")
                and _target_role_title(str(row["title"]))
                and _us_location_relevant(row.get("location"))
                and row.get("jobUrl")
            )
            return results[:limit]
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
            raise SearchProviderError(f"Ashby board {query!r} failed: {error}") from error


class CompanyCareerPageProvider:
    """Generic public career-page monitor for companies with custom systems."""

    name = "company_career"

    def __init__(self, pages: dict[str, str], **client_options: object) -> None:
        self.pages = pages
        self.transport = client_options.get("transport")
        self.timeout_seconds = float(
            client_options.get("timeout_seconds", DEFAULT_SEARCH_TIMEOUT_SECONDS)
        )

    async def search(self, query: str, *, limit: int) -> tuple[SearchResult, ...]:
        company = _configured_company(self.pages, query, "company careers")
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": "JobRadar/0.1 (+personal job discovery)"},
            ) as client:
                response = await client.get(query)
                response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            results: list[SearchResult] = []
            seen: set[str] = set()
            job_like_links = 0
            for anchor in soup.select("a[href]"):
                href = urljoin(str(response.url), anchor.get("href", ""))
                title = " ".join(anchor.get_text(" ").split())
                path = urlsplit(href).path.casefold()
                if not title or not re.search(
                    r"/(?:job|jobs|career|careers|details|positions?)/", path
                ):
                    continue
                job_like_links += 1
                if not _target_role_title(title):
                    continue
                if href in seen:
                    continue
                seen.add(href)
                results.append(
                    SearchResult(title=title, url=href, source=company, company=company)
                )
                if len(results) >= limit:
                    break
            if not job_like_links:
                raise SearchProviderError(
                    f"Company career page {query!r} exposed no job links; "
                    "it may require a dedicated JavaScript/API adapter."
                )
            return tuple(results)
        except SearchProviderError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise SearchProviderError(f"Company career page {query!r} failed: {error}") from error


class PriorityCompanySearchProvider:
    """Bounded public search scoped to one configured company career domain."""

    name = "priority_company_search"

    def __init__(
        self,
        pages: dict[str, str],
        *,
        search_provider: SearchProvider | None = None,
    ) -> None:
        self.pages = pages
        self.search_provider = search_provider or BraveSearchProvider()

    async def search(self, query: str, *, limit: int) -> tuple[SearchResult, ...]:
        company = _configured_company(self.pages, query, "priority company")
        host = urlsplit(query).netloc.casefold().removeprefix("www.")
        if not host:
            raise SearchProviderError(f"Priority company URL {query!r} has no host.")
        role_clause = (
            '("software engineer" OR "platform engineer" OR '
            '"infrastructure engineer" OR SDET OR "quality automation" OR '
            '"applied AI engineer" OR "AI automation engineer" OR '
            '"forward deployed engineer")'
        )
        # Career pages frequently keep the location outside the indexed title or
        # snippet. Requiring a US term here hid valid Apple, Microsoft, and Meta
        # roles. The extractor and US policy layer remain the authoritative
        # location gate after discovery.
        search_query = f"site:{host} {role_clause} jobs"
        results = await self.search_provider.search(search_query, limit=limit)
        return tuple(
            result.model_copy(
                update={
                    "company": company,
                    "source": f"{company} careers search",
                }
            )
            for result in results
            if _target_role_title(result.title)
            and _same_or_subdomain(result.url, host)
        )


def create_search_provider(name: str | None = None) -> SearchProvider:
    provider = (name or os.getenv("JOB_SEARCH_PROVIDER", "brave")).casefold()
    if provider == "brave":
        return BraveSearchProvider()
    if provider == "serpapi":
        return SerpApiSearchProvider()
    if provider in {"google_jobs", "serpapi_google_jobs"}:
        return SerpApiGoogleJobsProvider()
    raise ValueError(
        "JOB_SEARCH_PROVIDER must be 'brave', 'serpapi', or 'serpapi_google_jobs'."
    )


def _google_jobs_result(row: dict[str, object]) -> SearchResult:
    options = row.get("apply_options")
    apply_options = options if isinstance(options, list) else []
    company = str(row["company_name"])
    url = _preferred_apply_url(apply_options, company=company)
    if not url:
        job_id = str(row.get("job_id") or "")
        url = f"https://www.google.com/search?q={job_id}" if job_id else ""
    description = str(row.get("description") or "").strip()
    extensions = row.get("detected_extensions")
    detected = extensions if isinstance(extensions, dict) else {}
    location = str(row.get("location") or "").strip()
    return SearchResult(
        title=str(row["title"]),
        url=url,
        snippet=description,
        source=str(row.get("via") or "Google Jobs"),
        source_job_id=str(row.get("job_id") or "") or None,
        company=company,
        description=description or None,
        location=location or None,
        employment_type=str(detected.get("schedule_type") or "") or None,
        workplace_type=("Remote" if "remote" in location.casefold() else None),
        canonical_content=bool(description),
    )


def _preferred_apply_url(options: list[object], *, company: str | None = None) -> str:
    links = [
        (str(option.get("title") or ""), str(option.get("link")))
        for option in options
        if isinstance(option, dict) and option.get("link")
    ]
    if not links:
        return ""

    def rank(item: tuple[str, str]) -> tuple[int, str]:
        title, url = item
        host = urlsplit(url).netloc.casefold()
        target = url.casefold()
        title_hint = title.casefold()
        if any(marker in target for marker in CANONICAL_JOB_HOST_MARKERS):
            return (0, url)
        if (
            company
            and _company_name_matches(company, title)
            and not any(marker in host for marker in AGGREGATOR_HOSTS)
        ):
            return (1, url)
        if any(word in title_hint for word in ("company", "employer", "career")):
            return (1, url)
        if any(marker in host for marker in AGGREGATOR_HOSTS):
            return (3, url)
        return (2, url)

    return min(links, key=rank)[1]


def _company_name_matches(configured: str, discovered: str) -> bool:
    configured_name = " ".join(re.findall(r"[a-z0-9]+", configured.casefold()))
    discovered_name = " ".join(re.findall(r"[a-z0-9]+", discovered.casefold()))
    return bool(
        configured_name == discovered_name
        or discovered_name.startswith(configured_name + " ")
        or configured_name.startswith(discovered_name + " ")
    )


def _same_or_subdomain(url: str, expected_host: str) -> bool:
    discovered = urlsplit(url).netloc.casefold().removeprefix("www.")
    normalized_expected = expected_host.casefold().removeprefix("www.")
    return bool(
        discovered == normalized_expected
        or discovered.endswith("." + normalized_expected)
    )


def _lever_description(row: dict[str, object]) -> str:
    parts = [
        str(row.get("descriptionPlain") or row.get("description") or ""),
        str(row.get("additionalPlain") or row.get("additional") or ""),
    ]
    lists = row.get("lists")
    if isinstance(lists, list):
        for item in lists:
            if isinstance(item, dict):
                parts.extend((str(item.get("text") or ""), str(item.get("content") or "")))
    return _plain_text("\n".join(parts))


def _ashby_result(row: dict[str, object], company: str) -> SearchResult:
    compensation = row.get("compensation")
    minimum = maximum = None
    if isinstance(compensation, dict):
        components = compensation.get("summaryComponents")
        if isinstance(components, list):
            salary = next(
                (
                    item
                    for item in components
                    if isinstance(item, dict)
                    and item.get("compensationType") == "Salary"
                    and item.get("currencyCode") == "USD"
                    and item.get("interval") == "1 YEAR"
                ),
                None,
            )
            if salary:
                minimum = salary.get("minValue")
                maximum = salary.get("maxValue")
    job_url = str(row["jobUrl"])
    source_job_id = urlsplit(job_url).path.rstrip("/").split("/")[-1]
    return SearchResult(
        title=str(row["title"]),
        url=job_url,
        source=company,
        source_job_id=source_job_id or None,
        ats_name="ashby",
        company=company,
        description=str(row.get("descriptionPlain") or ""),
        location=str(row.get("location") or "") or None,
        employment_type=str(row.get("employmentType") or "") or None,
        workplace_type=str(row.get("workplaceType") or "") or None,
        base_salary_min_usd=(round(minimum) if isinstance(minimum, (int, float)) else None),
        base_salary_max_usd=(round(maximum) if isinstance(maximum, (int, float)) else None),
        date_posted=str(row.get("publishedAt") or "") or None,
        canonical_content=True,
    )


def _configured_company(mapping: dict[str, str], key: str, label: str) -> str:
    company = mapping.get(key)
    if not company:
        raise SearchProviderError(f"Unknown {label} source {key!r}.")
    return company


def _plain_text(value: object) -> str:
    return " ".join(
        BeautifulSoup(html.unescape(str(value or "")), "html.parser")
        .get_text(" ")
        .split()
    )


def _require_key(value: str | None, name: str) -> None:
    if not value or value.startswith("replace_with_"):
        raise SearchProviderError(f"{name} is not configured.")


def _target_role_title(title: str) -> bool:
    normalized = " ".join(title.casefold().split())
    patterns = (
        r"\b(?:senior|staff|sr\.?|principal)?\s*software engineer\b",
        r"\bsoftware engineer\s+(?:iii|3)\b",
        r"\b(?:senior|staff|sr\.?|principal)\s+(?:backend|full[- ]?stack) engineer\b",
        r"\b(?:platform|infrastructure|developer experience) engineer\b",
        r"\bSDET\b",
        r"\bsoftware development engineer in test\b",
        r"\bquality automation engineer\b",
        r"\bautomation quality engineer\b",
        r"\bautomation engineer\b",
        r"\b(?:AI automation|applied AI) engineer\b",
        r"\bforward deployed (?:software )?engineer\b",
        r"\b(?:FDE|frontier agents engineer)\b",
    )
    return any(re.search(pattern, normalized, re.IGNORECASE) for pattern in patterns)


def _us_location_relevant(value: object) -> bool:
    """Exclude explicit international-only board rows; retain unknown locations."""

    location = " ".join(str(value or "").split())
    if not location or location.casefold() == "remote":
        return True
    return is_us_based(location)
