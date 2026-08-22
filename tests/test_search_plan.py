"""Tests for role, location, company, and source query planning."""

from agent.search_plan import (
    GAP_SOURCE_CLAUSE,
    LOCATION_TIERS,
    ROLE_FAMILIES,
    brave_gap_queries,
    configured_discovery_tracks,
    configured_mapping,
    google_jobs_queries,
)


def test_google_jobs_queries_cover_every_role_and_location_without_sponsorship() -> None:
    queries = google_jobs_queries()

    assert len(queries) == len(ROLE_FAMILIES) * len(LOCATION_TIERS)
    assert any("Los Angeles" in query for query in queries)
    assert any("Remote, United States" in query for query in queries)
    assert all("sponsorship" not in query.casefold() for query in queries)


def test_brave_gap_queries_explicitly_cover_linkedin_indeed_and_ziprecruiter() -> None:
    queries = brave_gap_queries(
        priority_companies=("Apple",), fde_companies=("Glean",)
    )

    assert "linkedin.com/jobs/view" in GAP_SOURCE_CLAUSE
    assert "indeed.com/viewjob" in GAP_SOURCE_CLAUSE
    assert "ziprecruiter.com/jobs" in GAP_SOURCE_CLAUSE
    assert any('"Apple"' in query for query in queries)
    assert any('"Glean"' in query for query in queries)


def test_configured_tracks_build_three_track_sources(monkeypatch) -> None:
    monkeypatch.setenv("GREENHOUSE_BOARDS", "scaleai=Scale AI")
    monkeypatch.setenv("LEVER_BOARDS", "cohere=Cohere")
    monkeypatch.setenv("ASHBY_BOARDS", "glean=Glean")
    monkeypatch.setenv("CUSTOM_CAREER_PAGES", "Apple=https://jobs.apple.com/en-us/search")
    monkeypatch.setenv("SERPAPI_API_KEY", "serp-key")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")

    tracks = configured_discovery_tracks(result_limit=60)

    assert {track.name for track in tracks} == {
        "priority_greenhouse",
        "priority_lever",
        "priority_ashby",
        "priority_custom_careers",
        "google_jobs",
        "brave_gaps",
    }
    assert {track.name: track.interval_hours for track in tracks} == {
        "priority_greenhouse": 2,
        "priority_lever": 2,
        "priority_ashby": 2,
        "priority_custom_careers": 2,
        "google_jobs": 4,
        "brave_gaps": 12,
    }


def test_configured_mapping_reverses_company_career_entries(monkeypatch) -> None:
    monkeypatch.setenv("CUSTOM_CAREER_PAGES", "Apple=https://jobs.apple.com/search")

    assert configured_mapping("CUSTOM_CAREER_PAGES", reverse=True) == {
        "https://jobs.apple.com/search": "Apple"
    }
