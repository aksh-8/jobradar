"""Public-web contact discovery for recruiter and hiring-team suggestions."""

from __future__ import annotations

import asyncio
import html
import re
from enum import Enum
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from agent.search_providers import BraveSearchProvider, SearchProvider, SearchResult
from backend.red_flag_scanner import JobPostingFacts


class ContactType(str, Enum):
    RECRUITER = "RECRUITER"
    HIRING_MANAGER = "HIRING_MANAGER"
    TEAM_MEMBER = "TEAM_MEMBER"


class ContactSuggestion(BaseModel):
    """One public professional lead that must be manually verified."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str = Field(min_length=2)
    title: str = Field(min_length=2)
    contact_type: ContactType
    profile_url: str = Field(min_length=8)
    source: str = Field(min_length=2)
    confidence: int = Field(ge=0, le=100)
    evidence: str = Field(min_length=2)


class ContactDiscoveryError(RuntimeError):
    """Raised when public contact discovery cannot complete."""


class PublicContactFinder:
    """Find public professional leads without opening or scraping profile pages."""

    def __init__(self, search_provider: SearchProvider | None = None) -> None:
        self.search_provider = search_provider or BraveSearchProvider()

    async def find(
        self,
        posting: JobPostingFacts,
        *,
        job_url: str,
        limit: int = 3,
    ) -> tuple[ContactSuggestion, ...]:
        role_terms = _role_terms(posting.title)
        role_query = " OR ".join(f'"{term}"' for term in role_terms)
        job_host = urlsplit(job_url).netloc.casefold().removeprefix("www.")
        excluded_hosts = (
            "linkedin.com",
            "indeed.com",
            "ziprecruiter.com",
            "jobright.ai",
            "greenhouse.io",
            "lever.co",
            "ashbyhq.com",
            "myworkdayjobs.com",
        )
        public_team_scope = (
            f"site:{job_host}"
            if job_host and not any(marker in job_host for marker in excluded_hosts)
            else f'"{posting.company}"'
        )
        queries = (
            f'site:linkedin.com/in "{posting.company}" '
            '("technical recruiter" OR "engineering recruiter" OR '
            '"talent acquisition")',
            f'site:linkedin.com/in "{posting.company}" '
            '("engineering manager" OR "director of engineering" OR '
            f'"hiring manager") ({role_query})',
            f'{public_team_scope} ("engineering leadership" OR "engineering team") '
            f'({role_query}) -jobs',
        )
        try:
            result_groups = await asyncio.gather(
                *(self.search_provider.search(query, limit=8) for query in queries)
            )
        except Exception as error:
            raise ContactDiscoveryError(f"Public contact search failed: {error}") from error

        suggestions: dict[str, ContactSuggestion] = {}
        for result in (item for group in result_groups for item in group):
            suggestion = _contact_suggestion(
                result,
                company=posting.company,
                role_terms=role_terms,
            )
            if suggestion is None:
                continue
            existing = suggestions.get(suggestion.profile_url)
            if existing is None or suggestion.confidence > existing.confidence:
                suggestions[suggestion.profile_url] = suggestion
        ordered = sorted(
            suggestions.values(),
            key=lambda item: (
                _CONTACT_PRIORITY[item.contact_type],
                -item.confidence,
                item.name.casefold(),
            ),
        )
        return tuple(ordered[:limit])


_CONTACT_PRIORITY = {
    ContactType.RECRUITER: 0,
    ContactType.HIRING_MANAGER: 1,
    ContactType.TEAM_MEMBER: 2,
}
_GENERIC_ROLE_WORDS = {
    "senior",
    "staff",
    "lead",
    "principal",
    "software",
    "engineer",
    "engineering",
    "developer",
    "development",
    "level",
    "remote",
    "united",
    "states",
    "los",
    "angeles",
    "california",
    "usa",
}
_NON_NAME_WORDS = {
    "engineering",
    "leadership",
    "team",
    "careers",
    "jobs",
    "recruiter",
    "manager",
    "director",
    "company",
    "linkedin",
    "profile",
    "people",
    "talent",
    "acquisition",
}


def _contact_suggestion(
    result: SearchResult,
    *,
    company: str,
    role_terms: tuple[str, ...],
) -> ContactSuggestion | None:
    rendered_title = html.unescape(result.title).strip()
    rendered_snippet = html.unescape(result.snippet).strip()
    combined = f"{rendered_title} {rendered_snippet}".casefold()
    company_tokens = _company_tokens(company)
    if company_tokens and not any(token in combined for token in company_tokens):
        return None
    name, professional_title = _name_and_title(rendered_title, rendered_snippet)
    if not name or not professional_title:
        return None

    contact_type, base_confidence, match_label = _classify_contact(
        rendered_title.casefold(),
        role_terms,
    )
    if contact_type is None:
        return None
    role_matches = tuple(
        term for term in role_terms if term in rendered_title.casefold()
    )
    confidence = min(
        99,
        base_confidence
        + (5 if company_tokens else 0)
        + min(8, len(role_matches) * 3)
        + (3 if "linkedin.com/in/" in result.url.casefold() else 0),
    )
    source = (
        "LinkedIn public search result"
        if "linkedin.com/in/" in result.url.casefold()
        else f"Public company/web result ({urlsplit(result.url).netloc})"
    )
    role_evidence = f" Role terms: {', '.join(role_matches)}." if role_matches else ""
    return ContactSuggestion(
        name=name,
        title=professional_title,
        contact_type=contact_type,
        profile_url=_canonical_profile_url(result.url),
        source=source,
        confidence=confidence,
        evidence=(
            f"Public result associates this person with {company} and matched "
            f"{match_label}.{role_evidence} Verify current employment before outreach."
        ),
    )


def _classify_contact(
    text: str,
    role_terms: tuple[str, ...],
) -> tuple[ContactType | None, int, str]:
    if any(
        term in text
        for term in (
            "technical recruiter",
            "engineering recruiter",
            "talent acquisition",
            "recruiting",
        )
    ):
        return ContactType.RECRUITER, 86, "recruiting responsibility"
    if any(
        term in text
        for term in (
            "hiring manager",
            "engineering manager",
            "software engineering manager",
            "director of engineering",
            "head of engineering",
            "vp of engineering",
            "vice president of engineering",
        )
    ):
        return ContactType.HIRING_MANAGER, 82, "engineering-management responsibility"
    if any(term in text for term in role_terms):
        return ContactType.TEAM_MEMBER, 60, "relevant engineering-team experience"
    return None, 0, ""


def _name_and_title(title: str, snippet: str) -> tuple[str | None, str | None]:
    parts = [
        part.strip()
        for part in re.split(r"\s+(?:-|\||\u2013|\u2014)\s+", title)
        if part.strip()
    ]
    if not parts:
        return None, None
    candidate = parts[0]
    words = re.findall(r"[^\W\d_]+(?:['-][^\W\d_]+)*", candidate, re.UNICODE)
    if not 2 <= len(words) <= 5:
        return None, None
    if any(word.casefold() in _NON_NAME_WORDS for word in words):
        return None, None
    if any(not word[0].isupper() for word in words):
        return None, None
    professional_title = " / ".join(
        part for part in parts[1:] if "linkedin" not in part.casefold()
    )
    professional_title = professional_title or snippet
    professional_title = " ".join(professional_title.split())[:240].strip(" -|")
    return " ".join(words), professional_title or None


def _role_terms(title: str) -> tuple[str, ...]:
    tokens = [
        token
        for token in re.findall(r"[a-z0-9+#.]+", title.casefold())
        if len(token) > 2 and token not in _GENERIC_ROLE_WORDS
    ]
    phrases: list[str] = []
    normalized = title.casefold()
    for phrase in (
        "forward deployed",
        "platform",
        "infrastructure",
        "developer experience",
        "test automation",
        "sdet",
        "quality automation",
        "applied ai",
        "ai automation",
        "backend",
        "data infrastructure",
        "distributed systems",
    ):
        if phrase in normalized and phrase not in phrases:
            phrases.append(phrase)
    for token in tokens:
        if token not in phrases and not any(token in phrase for phrase in phrases):
            phrases.append(token)
    return tuple(phrases[:4] or ("software engineer",))


def _company_tokens(company: str) -> tuple[str, ...]:
    ignored = {
        "inc",
        "llc",
        "ltd",
        "company",
        "corporation",
        "corp",
        "global",
        "tech",
    }
    return tuple(
        token
        for token in re.findall(r"[a-z0-9]+", company.casefold())
        if len(token) > 2 and token not in ignored
    )


def _canonical_profile_url(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{parsed.path.rstrip('/')}"
