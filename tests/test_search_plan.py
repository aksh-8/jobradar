"""Tests for role, location, company, and source query planning."""

from datetime import date

from agent.search_plan import (
    GAP_SOURCE_CLAUSE,
    LOCATION_TIERS,
    ROLE_FAMILIES,
    DEFAULT_PRIORITY_COMPANIES,
    brave_gap_queries,
    configured_discovery_tracks,
    configured_mapping,
    google_jobs_queries,
    rotating_priority_page,
)


def test_google_jobs_queries_cover_every_role_and_location_without_sponsorship() -> None:
    queries = google_jobs_queries()

    assert len(queries) == len(ROLE_FAMILIES) * len(LOCATION_TIERS)
    assert any("Los Angeles" in query for query in queries)
    assert any("Remote, United States" in query for query in queries)
    assert LOCATION_TIERS == (
        "Los Angeles, California",
        "Remote, United States",
        "United States",
    )
    assert all("sponsorship" not in query.casefold() for query in queries)


def test_brave_gap_queries_explicitly_cover_linkedin_indeed_and_ziprecruiter() -> None:
    queries = brave_gap_queries()

    assert len(queries) == 7
    assert "linkedin.com/jobs/view" in GAP_SOURCE_CLAUSE
    assert "indeed.com/viewjob" in GAP_SOURCE_CLAUSE
    assert "ziprecruiter.com/jobs" in GAP_SOURCE_CLAUSE
    assert all('"United States"' in query for query in queries[:5])
    assert any("Los Angeles" in query for query in queries)
    assert any("US remote" in query for query in queries)
    assert all("sponsorship" not in query.casefold() for query in queries)
    assert {"NVIDIA", "Tesla"} <= set(DEFAULT_PRIORITY_COMPANIES)


def test_configured_tracks_build_multi_source_schedule(monkeypatch) -> None:
    monkeypatch.setenv("GREENHOUSE_BOARDS", "scaleai=Scale AI")
    monkeypatch.setenv("LEVER_BOARDS", "cohere=Cohere")
    monkeypatch.setenv("ASHBY_BOARDS", "glean=Glean")
    monkeypatch.setenv("CUSTOM_CAREER_PAGES", "Apple=https://jobs.apple.com/en-us/search")
    monkeypatch.setenv("SERPAPI_API_KEY", "serp-key")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setenv("ENABLE_SERPAPI_DISCOVERY", "false")
    monkeypatch.setenv("ENABLE_CUSTOM_CAREER_POLLING", "false")

    tracks = configured_discovery_tracks(result_limit=60)

    assert {track.name for track in tracks} == {
        "priority_greenhouse",
        "priority_lever",
        "priority_ashby",
        "brave_gaps",
        "apple_careers_search",
    }
    assert {track.name: track.interval_hours for track in tracks} == {
        "priority_greenhouse": 2,
        "priority_lever": 2,
        "priority_ashby": 2,
        "brave_gaps": 12,
        "apple_careers_search": 12,
    }


def test_serpapi_tracks_require_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "serp-key")
    monkeypatch.setenv("ENABLE_SERPAPI_DISCOVERY", "true")
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("GREENHOUSE_BOARDS", raising=False)
    monkeypatch.delenv("LEVER_BOARDS", raising=False)
    monkeypatch.delenv("ASHBY_BOARDS", raising=False)
    monkeypatch.delenv("CUSTOM_CAREER_PAGES", raising=False)

    tracks = configured_discovery_tracks(result_limit=60)

    assert {track.name for track in tracks} == {
        "priority_google_jobs",
        "google_jobs",
    }


def test_rotating_priority_page_is_stable_daily_and_excludes_apple(monkeypatch) -> None:
    monkeypatch.setenv(
        "PRIORITY_COMPANIES", "Apple|Google|Microsoft|Amazon"
    )
    pages = {
        "https://jobs.apple.com/search": "Apple",
        "https://google.example/jobs": "Google",
        "https://microsoft.example/jobs": "Microsoft",
        "https://amazon.example/jobs": "Amazon",
    }

    first = rotating_priority_page(pages, on_date=date(2026, 8, 26))
    same_day = rotating_priority_page(pages, on_date=date(2026, 8, 26))
    next_day = rotating_priority_page(pages, on_date=date(2026, 8, 27))

    assert first == same_day
    assert set(first.values()) <= {"Google", "Microsoft", "Amazon"}
    assert next_day != first


def test_configured_mapping_reverses_company_career_entries(monkeypatch) -> None:
    monkeypatch.setenv("CUSTOM_CAREER_PAGES", "Apple=https://jobs.apple.com/search")

    assert configured_mapping("CUSTOM_CAREER_PAGES", reverse=True) == {
        "https://jobs.apple.com/search": "Apple"
    }
