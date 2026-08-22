"""Fetch and normalize structured facts from job posting pages."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable

import httpx
from bs4 import BeautifulSoup

from agent.search_providers import SearchResult
from backend.red_flag_scanner import JobPostingFacts, SponsorshipStatus


class PostingExtractionError(RuntimeError):
    """Raised when a result page lacks the facts required for safe scoring."""


class PostingExtractor:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.transport = transport
        self.timeout_seconds = timeout_seconds or float(
            os.getenv("REQUEST_TIMEOUT_SECONDS", "30")
        )

    async def fetch(self, result: SearchResult) -> JobPostingFacts:
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": "JobRadar/0.1 (+personal job discovery)"},
            ) as client:
                response = await client.get(result.url)
                response.raise_for_status()
            return extract_posting(response.text, result)
        except PostingExtractionError:
            raise
        except httpx.HTTPError as error:
            raise PostingExtractionError(
                f"Could not fetch {result.url}: {error}"
            ) from error


def extract_posting(html: str, result: SearchResult) -> JobPostingFacts:
    soup = BeautifulSoup(html, "html.parser")
    structured = next(_job_postings(soup), {})
    title = _text(structured.get("title")) or _first_text(soup, ("h1", "title"))
    organization = structured.get("hiringOrganization")
    company = (
        _text(organization.get("name"))
        if isinstance(organization, dict)
        else _text(organization)
    )
    if not company:
        company = _first_text(
            soup,
            (
                "[data-company-name]",
                "[class*='company-name']",
                "[class*='companyName']",
            ),
        )
    description = _html_text(structured.get("description")) or _first_text(
        soup,
        (
            "[data-test-id='job-description']",
            "#jobDescriptionText",
            "[class*='job-description']",
            "[class*='jobDescription']",
            "main",
        ),
    )
    if not title or not company or not description:
        missing = [
            label
            for label, value in (
                ("title", title),
                ("company", company),
                ("description", description),
            )
            if not value
        ]
        raise PostingExtractionError(
            f"{result.url} is missing required fields: {', '.join(missing)}."
        )

    salary = _salary(structured.get("baseSalary"))
    searchable = f"{title}\n{company}\n{description}"
    return JobPostingFacts(
        title=title,
        company=company,
        description=description,
        sponsorship_status=_sponsorship(searchable),
        **salary,
    )


def _job_postings(soup: BeautifulSoup) -> Iterable[dict[str, object]]:
    for script in soup.select("script[type='application/ld+json']"):
        try:
            parsed = json.loads(script.string or script.get_text() or "null")
        except (json.JSONDecodeError, TypeError):
            continue
        queue = list(parsed) if isinstance(parsed, list) else [parsed]
        while queue:
            item = queue.pop(0)
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                queue.extend(graph)
            types = item.get("@type")
            type_values = types if isinstance(types, list) else [types]
            if "JobPosting" in type_values:
                yield item


def _first_text(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        element = soup.select_one(selector)
        if element and (value := _text(element.get_text(" "))):
            return value
    return ""


def _text(value: object) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _html_text(value: object) -> str:
    if value is None:
        return ""
    return _text(BeautifulSoup(str(value), "html.parser").get_text(" "))


def _salary(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    currency = _text(value.get("currency")).upper()
    if currency and currency != "USD":
        return {}
    amount = value.get("value", value)
    if not isinstance(amount, dict):
        return {}
    unit = _text(amount.get("unitText") or value.get("unitText")).upper()
    if unit and unit not in {"YEAR", "YEARLY", "ANNUAL"}:
        return {}
    output: dict[str, int] = {}
    minimum = amount.get("minValue", amount.get("value"))
    maximum = amount.get("maxValue", amount.get("value"))
    if isinstance(minimum, (int, float)) and minimum >= 0:
        output["base_salary_min_usd"] = round(minimum)
    if isinstance(maximum, (int, float)) and maximum >= 0:
        output["base_salary_max_usd"] = round(maximum)
    return output


def _sponsorship(text: str) -> SponsorshipStatus:
    normalized = text.casefold()
    negative = (
        "no visa sponsorship",
        "without sponsorship",
        "unable to offer sponsorship",
        "will not provide sponsorship",
        "not eligible for employment sponsorship",
    )
    if any(phrase in normalized for phrase in negative):
        return SponsorshipStatus.NO
    positive = (
        "visa sponsorship is available",
        "visa sponsorship available",
        "will provide visa sponsorship",
        "offer visa sponsorship",
    )
    if any(phrase in normalized for phrase in positive):
        return SponsorshipStatus.YES
    return SponsorshipStatus.UNKNOWN
