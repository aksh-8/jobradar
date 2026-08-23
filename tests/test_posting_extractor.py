"""Tests for server-side job page normalization."""

import httpx
import pytest

from agent.posting_extractor import (
    ClosedJobPostingError,
    PostingExtractionError,
    PostingExtractor,
    extract_posting,
)
from agent.search_providers import SearchResult
from backend.red_flag_scanner import SponsorshipStatus


def result() -> SearchResult:
    return SearchResult(title="Search title", url="https://example.com/jobs/1")


def test_extracts_json_ld_job_posting() -> None:
    html = """
    <script type="application/ld+json">
    {
      "@type": "JobPosting",
      "title": "Senior Backend Engineer",
      "hiringOrganization": {"name": "Acme & Co"},
      "description": "<p>Build Python services. Visa sponsorship is available.</p>",
      "employmentType": "FULL_TIME",
      "jobLocation": {
        "address": {
          "addressLocality": "Los Angeles",
          "addressRegion": "CA",
          "addressCountry": "US"
        }
      },
      "baseSalary": {
        "currency": "USD",
        "value": {"minValue": 150000, "maxValue": 190000, "unitText": "YEAR"}
      }
    }
    </script>
    """

    posting = extract_posting(html, result())

    assert posting.title == "Senior Backend Engineer"
    assert posting.company == "Acme & Co"
    assert posting.description == "Build Python services. Visa sponsorship is available."
    assert posting.sponsorship_status is SponsorshipStatus.YES
    assert posting.base_salary_max_usd == 190_000
    assert posting.location == "Los Angeles, CA, US"
    assert posting.employment_type == "FULL_TIME"


def test_falls_back_to_common_dom_fields() -> None:
    posting = extract_posting(
        """
        <main>
          <h1>Platform Engineer</h1>
          <div data-company-name>Canopy</div>
          <div class="job-description">No visa sponsorship for this position.</div>
        </main>
        """,
        result(),
    )

    assert posting.company == "Canopy"
    assert posting.sponsorship_status is SponsorshipStatus.NO


def test_extracts_efinancialcareers_company_info() -> None:
    posting = extract_posting(
        """
        <main>
          <h1>Lead Software Engineer - Python/Automation</h1>
          <span class="companyInfo"> JPMorgan Chase &amp; Co. </span>
          <span class="loc"> Plano, United States </span>
          <div class="job-description">Build secure Python services.</div>
        </main>
        """,
        result(),
    )

    assert posting.company == "JPMorgan Chase & Co."
    assert posting.location == "Plano, United States"


def test_rejects_pages_without_required_fields() -> None:
    with pytest.raises(PostingExtractionError, match="company, description"):
        extract_posting("<h1>Backend Engineer</h1>", result())


def test_rejects_closed_jobright_style_page_before_scoring() -> None:
    html = """
    <main>
      <p>This job has closed.</p>
      <h1>Software Engineer, Level 3</h1>
      <div data-company-name>Snap Inc.</div>
      <div class="job-description">Build scalable backend services.</div>
    </main>
    """

    with pytest.raises(ClosedJobPostingError, match="This job has closed"):
        extract_posting(html, result())


def test_rejects_expired_structured_posting() -> None:
    html = """
    <script type="application/ld+json">
    {
      "@type": "JobPosting",
      "title": "Backend Engineer",
      "hiringOrganization": {"name": "Acme"},
      "description": "Build APIs.",
      "validThrough": "2020-01-01"
    }
    </script>
    """

    with pytest.raises(ClosedJobPostingError, match="expired"):
        extract_posting(html, result())


@pytest.mark.asyncio
async def test_authoritative_feed_content_does_not_refetch_page() -> None:
    posting = await PostingExtractor().fetch(
        SearchResult(
            title="Platform Engineer",
            url="https://jobs.ashbyhq.com/acme/7",
            company="Acme",
            description="Build cloud systems. Visa sponsorship available.",
            location="Remote, United States",
            workplace_type="Remote",
            canonical_content=True,
        )
    )

    assert posting.company == "Acme"
    assert posting.location == "Remote, United States"
    assert posting.sponsorship_status is SponsorshipStatus.YES


@pytest.mark.asyncio
async def test_public_page_fetch_uses_browser_compatible_headers() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"].startswith("Mozilla/5.0")
        assert "text/html" in request.headers["accept"]
        return httpx.Response(
            200,
            text="""
            <main>
              <h1>Platform Engineer</h1>
              <span class="companyInfo">Acme</span>
              <div class="job-description">Build Python services.</div>
            </main>
            """,
        )

    posting = await PostingExtractor(
        transport=httpx.MockTransport(handler)
    ).fetch(result())

    assert posting.company == "Acme"
