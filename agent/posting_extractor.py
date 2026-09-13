"""Fetch and normalize structured facts from job posting pages."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from agent.search_providers import SearchResult
from backend.job_availability import closure_reason
from backend.red_flag_scanner import JobPostingFacts, SponsorshipStatus

PUBLIC_PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class PostingExtractionError(RuntimeError):
    """Raised when a result page lacks the facts required for safe scoring."""


class ClosedJobPostingError(PostingExtractionError):
    """Raised when authoritative evidence says a posting is unavailable."""


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
        if result.canonical_content:
            if not result.company or not result.description:
                raise PostingExtractionError(
                    f"Authoritative source {result.url} omitted company or description."
                )
            searchable = f"{result.title}\n{result.company}\n{result.description}"
            if reason := closure_reason(
                valid_through=result.valid_through,
                page_text=searchable,
            ):
                raise ClosedJobPostingError(f"{result.url} is closed: {reason}")
            return JobPostingFacts(
                title=result.title,
                company=result.company,
                description=result.description,
                location=result.location,
                employment_type=result.employment_type,
                workplace_type=result.workplace_type,
                date_posted=result.date_posted,
                valid_through=result.valid_through,
                sponsorship_status=_sponsorship(searchable),
                base_salary_min_usd=result.base_salary_min_usd,
                base_salary_max_usd=result.base_salary_max_usd,
            )
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers=PUBLIC_PAGE_HEADERS,
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
    page_text = soup.get_text(" ")
    apple_posting = _apple_job_posting(html, result)
    if _is_apple_job_url(result.url) and not apple_posting and re.search(
        r"\b(?:page|job|role) not found\b", page_text, re.IGNORECASE
    ):
        raise ClosedJobPostingError(
            f"{result.url} is closed: Apple Careers reports the page was not found."
        )
    structured = apple_posting or next(_job_postings(soup), {})
    valid_through = _text(structured.get("validThrough")) or None
    if reason := closure_reason(
        valid_through=valid_through,
        page_text=page_text,
    ):
        raise ClosedJobPostingError(f"{result.url} is closed: {reason}")
    title = (
        _text(structured.get("title"))
        or _first_text(soup, ("h1",))
        or result.title
        or _first_text(soup, ("title",))
    )
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
                ".companyInfo",
                "[class*='company-name']",
                "[class*='companyName']",
                "[class*='company-info']",
            ),
        )
    company = company or result.company or ""
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
    description = description or result.description or ""
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
    location, workplace_type = _location(structured)
    location = location or result.location or _first_text(
        soup,
        (
            "[data-job-location]",
            ".job-location",
            "[class*='jobLocation']",
            ".loc",
        ),
    )
    searchable = f"{title}\n{company}\n{description}"
    return JobPostingFacts(
        title=title,
        company=company,
        description=description,
        location=location,
        employment_type=_employment_type(structured.get("employmentType")),
        workplace_type=workplace_type,
        date_posted=_text(structured.get("datePosted")) or None,
        valid_through=valid_through,
        sponsorship_status=_sponsorship(searchable),
        **salary,
    )


def _is_apple_job_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return host == "jobs.apple.com" or host.endswith(".jobs.apple.com")


def _apple_job_posting(html: str, result: SearchResult) -> dict[str, object]:
    """Convert Apple's embedded router payload into JobPosting JSON-LD shape."""

    if not _is_apple_job_url(result.url):
        return {}
    match = re.search(
        r"window\.__staticRouterHydrationData\s*=\s*JSON\.parse\("
        r"(?P<encoded>\"(?:\\.|[^\"\\])*\")\)",
        html,
    )
    if not match:
        return {}
    try:
        payload = json.loads(json.loads(match.group("encoded")))
    except (json.JSONDecodeError, TypeError):
        return {}

    requested_job_number = ""
    if url_match := re.search(r"/details/(\d+)(?:/|$)", result.url):
        requested_job_number = url_match.group(1)
    job = _find_apple_job(payload, requested_job_number)
    if not job:
        return {}

    localized_posting = _apple_localized_posting(job)
    description_parts: list[str] = []
    for key in (
        "jobSummary",
        "description",
        "responsibilities",
        "minimumQualifications",
        "preferredQualifications",
    ):
        value = _html_text(localized_posting.get(key) or job.get(key))
        if value and value not in description_parts:
            description_parts.append(value)

    structured: dict[str, object] = {
        "@type": "JobPosting",
        "title": _text(
            localized_posting.get("postingTitle") or job.get("postingTitle")
        ),
        "hiringOrganization": {"name": "Apple"},
        "description": "\n\n".join(description_parts),
        "employmentType": job.get("employmentType"),
        "datePosted": job.get("postDateInGMT") or job.get("postingDate"),
    }
    if job.get("homeOffice"):
        structured["jobLocationType"] = "TELECOMMUTE"
    if locations := _apple_locations(job.get("localeLocation")):
        structured["jobLocation"] = locations
    if salary := _apple_salary(job.get("postingFooters")):
        structured["baseSalary"] = salary
    return structured


def _find_apple_job(payload: object, requested_job_number: str) -> dict[str, object]:
    fallback: dict[str, object] = {}
    queue = [payload]
    while queue:
        item = queue.pop(0)
        if isinstance(item, list):
            queue.extend(item)
            continue
        if not isinstance(item, dict):
            continue
        job_number = _text(item.get("jobNumber") or item.get("positionId"))
        if job_number and item.get("localizations"):
            if requested_job_number and job_number == requested_job_number:
                return item
            if not fallback:
                fallback = item
        queue.extend(item.values())
    return fallback if not requested_job_number else {}


def _apple_localized_posting(job: dict[str, object]) -> dict[str, object]:
    localizations = job.get("localizations")
    if not isinstance(localizations, dict):
        return {}
    preferred_locales = (job.get("selectedLocale"), "en_US", "en-US")
    for locale in preferred_locales:
        localized = localizations.get(locale) if isinstance(locale, str) else None
        if isinstance(localized, dict) and isinstance(localized.get("posting"), dict):
            return localized["posting"]
    for localized in localizations.values():
        if isinstance(localized, dict) and isinstance(localized.get("posting"), dict):
            return localized["posting"]
    return {}


def _apple_locations(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    locations: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict) or item.get("active") is False:
            continue
        address = {
            "addressLocality": item.get("city") or item.get("name"),
            "addressRegion": item.get("stateProvince"),
            "addressCountry": item.get("countryName"),
        }
        if any(_text(part) for part in address.values()):
            locations.append({"address": address})
    return locations


def _apple_salary(value: object) -> dict[str, object]:
    if not isinstance(value, list):
        return {}
    text_parts: list[str] = []
    for footer in value:
        if not isinstance(footer, dict):
            continue
        localizations = footer.get("localizations")
        if not isinstance(localizations, dict):
            continue
        for entries in localizations.values():
            entry_list = entries if isinstance(entries, list) else [entries]
            for entry in entry_list:
                if isinstance(entry, dict):
                    text_parts.append(_html_text(entry.get("content")))
    salary_match = re.search(
        r"base pay range[^$]{0,100}\$([\d,]+)\s+(?:and|to|[-\u2013])\s+\$([\d,]+)",
        " ".join(text_parts),
        re.IGNORECASE,
    )
    if not salary_match:
        return {}
    minimum, maximum = (
        int(value.replace(",", "")) for value in salary_match.groups()
    )
    return {
        "currency": "USD",
        "value": {
            "minValue": minimum,
            "maxValue": maximum,
            "unitText": "YEAR",
        },
    }


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


def _location(structured: dict[str, object]) -> tuple[str | None, str | None]:
    job_location_type = _text(structured.get("jobLocationType")).upper()
    workplace_type = "Remote" if "TELECOMMUTE" in job_location_type else None
    raw_locations = structured.get("jobLocation")
    locations = raw_locations if isinstance(raw_locations, list) else [raw_locations]
    rendered: list[str] = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        address = location.get("address", location)
        if isinstance(address, dict):
            parts = (
                _text(address.get("addressLocality")),
                _text(address.get("addressRegion")),
                _text(address.get("addressCountry")),
            )
            value = ", ".join(part for part in parts if part)
            if value and value not in rendered:
                rendered.append(value)
    if workplace_type and not rendered:
        requirements = structured.get("applicantLocationRequirements")
        requirement_items = requirements if isinstance(requirements, list) else [requirements]
        for requirement in requirement_items:
            if isinstance(requirement, dict):
                name = _text(requirement.get("name"))
                if name and name not in rendered:
                    rendered.append(name)
    return ("; ".join(rendered) or workplace_type, workplace_type)


def _employment_type(value: object) -> str | None:
    if isinstance(value, list):
        rendered = ", ".join(_text(item) for item in value if _text(item))
        return rendered or None
    return _text(value) or None


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
