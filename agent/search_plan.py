"""Configuration and query planning for JobRadar's three discovery tracks."""

from __future__ import annotations

import os
from dataclasses import dataclass

from agent.search_providers import (
    AshbyBoardProvider,
    BraveSearchProvider,
    CompanyCareerPageProvider,
    GreenhouseBoardProvider,
    LeverBoardProvider,
    SearchProvider,
    SerpApiGoogleJobsProvider,
)

ROLE_FAMILIES = (
    '("senior software engineer" OR "software engineer III" OR "staff software engineer")',
    '("platform engineer" OR "infrastructure engineer" OR "developer experience engineer")',
    '(SDET OR "software development engineer in test" OR "quality automation engineer")',
    '("AI automation engineer" OR "applied AI engineer")',
    '("forward deployed engineer" OR "forward deployed software engineer")',
)

LOCATION_TIERS = (
    "Los Angeles, California",
    "Greater Los Angeles, California",
    "Remote, United States",
    "California",
    "East Coast, United States",
    "United States",
)

DEFAULT_PRIORITY_COMPANIES = ("Apple", "Google", "Microsoft", "Amazon", "Meta")
DEFAULT_FDE_COMPANIES = ("Glean", "Cohere", "Scale AI")
GAP_SOURCE_CLAUSE = (
    "(site:linkedin.com/jobs/view OR site:indeed.com/viewjob OR "
    "site:ziprecruiter.com/jobs OR site:myworkdayjobs.com OR "
    "site:jobs.smartrecruiters.com)"
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


def brave_gap_queries(
    *,
    priority_companies: tuple[str, ...] | None = None,
    fde_companies: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Build source and company searches, explicitly including LinkedIn."""

    priority = priority_companies or configured_values(
        "PRIORITY_COMPANIES", DEFAULT_PRIORITY_COMPANIES
    )
    fde = fde_companies or configured_values(
        "FDE_TARGET_COMPANIES", DEFAULT_FDE_COMPANIES
    )
    company_clause = " OR ".join(f'"{company}"' for company in priority)
    fde_clause = " OR ".join(f'"{company}"' for company in fde)
    queries = [f"{role} {GAP_SOURCE_CLAUSE}" for role in ROLE_FAMILIES]
    queries.extend(
        f"{role} ({company_clause}) careers" for role in ROLE_FAMILIES[:-1]
    )
    queries.append(f'{ROLE_FAMILIES[-1]} ({fde_clause}) careers')
    return tuple(queries)


def configured_discovery_tracks(*, result_limit: int = 60) -> tuple[DiscoveryTrack, ...]:
    """Create only tracks whose required source settings are available."""

    tracks: list[DiscoveryTrack] = []
    greenhouse = configured_mapping("GREENHOUSE_BOARDS")
    lever = configured_mapping("LEVER_BOARDS")
    ashby = configured_mapping("ASHBY_BOARDS")
    career_pages = configured_mapping("CUSTOM_CAREER_PAGES", reverse=True)

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
    if career_pages:
        tracks.append(
            DiscoveryTrack(
                "priority_custom_careers", 2,
                CompanyCareerPageProvider(career_pages), tuple(career_pages),
                max(result_limit, 100) * len(career_pages),
            )
        )

    if _real_key(os.getenv("SERPAPI_API_KEY")):
        tracks.append(
            DiscoveryTrack(
                "google_jobs", 4, SerpApiGoogleJobsProvider(),
                google_jobs_queries(), result_limit,
            )
        )
    if _real_key(os.getenv("BRAVE_SEARCH_API_KEY")):
        tracks.append(
            DiscoveryTrack(
                "brave_gaps", 12, BraveSearchProvider(), brave_gap_queries(), result_limit,
            )
        )
    return tuple(tracks)


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
