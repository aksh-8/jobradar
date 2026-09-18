"""Configuration and query planning for JobRadar's three discovery tracks."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date

from agent.search_providers import (
    AshbyBoardProvider,
    BraveSearchProvider,
    CompanyCareerPageProvider,
    GreenhouseBoardProvider,
    LeverBoardProvider,
    PriorityCompanySearchProvider,
    PriorityGoogleJobsProvider,
    SearchProvider,
    SerpApiGoogleJobsProvider,
)

ROLE_FAMILIES = (
    '("software engineer II" OR "software engineer III" OR "senior software engineer")',
    '("platform engineer" OR "infrastructure engineer" OR "developer experience engineer")',
    '(SDET OR "software development engineer in test" OR "quality automation engineer")',
    '("AI automation engineer" OR "applied AI engineer")',
    '("forward deployed engineer" OR "forward deployed software engineer")',
)

LOCATION_TIERS = (
    "Los Angeles, California",
    "Remote, United States",
    "United States",
)

DEFAULT_PRIORITY_COMPANIES = (
    "Apple",
    "Google",
    "Microsoft",
    "Amazon",
    "Meta",
    "NVIDIA",
    "Tesla",
)
GAP_SOURCE_CLAUSE = (
    "(site:linkedin.com/jobs/view OR site:indeed.com/viewjob OR "
    "site:ziprecruiter.com/jobs OR site:myworkdayjobs.com OR "
    "site:jobs.smartrecruiters.com OR careers)"
)
COMBINED_ROLE_CLAUSE = (
    '("senior software engineer" OR "staff software engineer" OR '
    '"platform engineer" OR "infrastructure engineer" OR '
    '"developer experience engineer" OR SDET OR "quality automation engineer" OR '
    '"AI automation engineer" OR "applied AI engineer" OR '
    '"forward deployed engineer")'
)


@dataclass(frozen=True)
class DiscoveryTrack:
    """One independently scheduled provider/query group."""

    name: str
    interval_hours: int
    provider: SearchProvider
    queries: tuple[str, ...]
    result_limit: int


def google_jobs_queries() -> tuple[str, ...]:
    """Build role-by-location searches without sponsorship as a required term."""

    return tuple(
        f"{role} jobs in {location}"
        for role in ROLE_FAMILIES
        for location in LOCATION_TIERS
    )


def brave_gap_queries() -> tuple[str, ...]:
    """Build seven broad US searches for Brave's twice-daily gap track."""

    nationwide = tuple(
        f'{role} jobs "United States" {GAP_SOURCE_CLAUSE}'
        for role in ROLE_FAMILIES
    )
    return nationwide + (
        f'{COMBINED_ROLE_CLAUSE} jobs ("Los Angeles" OR "Culver City" OR "Irvine")',
        f'{COMBINED_ROLE_CLAUSE} jobs (remote "United States" OR "US remote")',
    )


def configured_discovery_tracks(*, result_limit: int = 60) -> tuple[DiscoveryTrack, ...]:
    """Create only tracks whose required source settings are available."""

    tracks: list[DiscoveryTrack] = []
    greenhouse = configured_mapping("GREENHOUSE_BOARDS")
    lever = configured_mapping("LEVER_BOARDS")
    ashby = configured_mapping("ASHBY_BOARDS")
    career_pages = configured_mapping("CUSTOM_CAREER_PAGES", reverse=True)
    priority_names = {
        company.casefold()
        for company in configured_values(
            "PRIORITY_COMPANIES", DEFAULT_PRIORITY_COMPANIES
        )
    }
    priority_pages = {
        url: company
        for url, company in career_pages.items()
        if company.casefold() in priority_names
    }

    if greenhouse:
        tracks.append(
            DiscoveryTrack(
                "priority_greenhouse", 2, GreenhouseBoardProvider(greenhouse),
                tuple(greenhouse), max(result_limit, 100) * len(greenhouse),
            )
        )
    if lever:
        tracks.append(
            DiscoveryTrack(
                "priority_lever", 2, LeverBoardProvider(lever), tuple(lever),
                max(result_limit, 100) * len(lever),
            )
        )
    if ashby:
        tracks.append(
            DiscoveryTrack(
                "priority_ashby", 2, AshbyBoardProvider(ashby), tuple(ashby),
                max(result_limit, 100) * len(ashby),
            )
        )
    if career_pages and _enabled("ENABLE_CUSTOM_CAREER_POLLING"):
        tracks.append(
            DiscoveryTrack(
                "priority_custom_careers", 2,
                CompanyCareerPageProvider(career_pages), tuple(career_pages),
                max(result_limit, 100) * len(career_pages),
            )
        )

    if (
        _enabled("ENABLE_SERPAPI_DISCOVERY")
        and _real_key(os.getenv("SERPAPI_API_KEY"))
    ):
        priority_companies = tuple(
            company
            for company in configured_values(
                "PRIORITY_COMPANIES", DEFAULT_PRIORITY_COMPANIES
            )
            if company.casefold() in priority_names
        )
        if priority_companies:
            tracks.append(
                DiscoveryTrack(
                    "priority_google_jobs",
                    4,
                    PriorityGoogleJobsProvider(priority_companies),
                    priority_companies,
                    max(result_limit, 5 * len(priority_companies)),
                )
            )
        tracks.append(
            DiscoveryTrack(
                "google_jobs", 4, SerpApiGoogleJobsProvider(),
                google_jobs_queries(), result_limit,
            )
        )
    if _real_key(os.getenv("BRAVE_SEARCH_API_KEY")):
        tracks.append(
            DiscoveryTrack(
                "brave_gaps", 12, BraveSearchProvider(freshness="pd"), brave_gap_queries(), result_limit,
            )
        )
        apple_pages = {
            url: company
            for url, company in priority_pages.items()
            if company.casefold() == "apple"
        }
        if apple_pages:
            tracks.append(
                DiscoveryTrack(
                    "apple_careers_search",
                    12,
                    PriorityCompanySearchProvider(apple_pages),
                    tuple(apple_pages),
                    10,
                )
            )
        rotating_pages = rotating_priority_page(priority_pages)
        if rotating_pages:
            tracks.append(
                DiscoveryTrack(
                    "rotating_priority_search",
                    12,
                    PriorityCompanySearchProvider(rotating_pages),
                    tuple(rotating_pages),
                    10,
                )
            )
    return tuple(tracks)


def rotating_priority_page(
    pages: dict[str, str], *, on_date: date | None = None
) -> dict[str, str]:
    """Choose one non-Apple priority career domain, stable for a calendar day."""

    configured_order = configured_values(
        "PRIORITY_COMPANIES", DEFAULT_PRIORITY_COMPANIES
    )
    by_company = {
        company.casefold(): (url, company) for url, company in pages.items()
    }
    candidates = [
        by_company[company.casefold()]
        for company in configured_order
        if company.casefold() != "apple" and company.casefold() in by_company
    ]
    if not candidates:
        return {}
    selected = candidates[(on_date or date.today()).toordinal() % len(candidates)]
    return {selected[0]: selected[1]}


def configured_values(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    values = tuple(value.strip() for value in raw.split("|") if value.strip())
    return values or default


def configured_mapping(name: str, *, reverse: bool = False) -> dict[str, str]:
    """Parse ``key=Company`` or, when reversed, ``Company=URL`` entries."""

    output: dict[str, str] = {}
    for entry in configured_values(name):
        if "=" not in entry:
            raise ValueError(f"{name} entries must use name=value syntax: {entry!r}.")
        left, right = (part.strip() for part in entry.split("=", 1))
        if not left or not right:
            raise ValueError(f"{name} entries cannot have blank names or values.")
        key, value = (right, left) if reverse else (left, right)
        if key in output:
            raise ValueError(f"{name} contains duplicate key {key!r}.")
        output[key] = value
    return output


def _real_key(value: str | None) -> bool:
    return bool(value and not value.startswith("replace_with_"))


def _enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().casefold() in {"1", "true", "yes", "on"}
