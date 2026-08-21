"""Deterministic hard-filter scanning for JobRadar job postings."""

from __future__ import annotations

import re
from enum import Enum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_MINIMUM_BASE_SALARY_USD = 140_000
DEFAULT_MINIMUM_COMPANY_SIZE = 50


class SponsorshipStatus(str, Enum):
    """Known employer sponsorship policy for a job posting."""

    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"


class FlagSeverity(str, Enum):
    """Whether a signal rejects a job or requires human review."""

    HARD_FILTER = "HARD_FILTER"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class RedFlagCode(str, Enum):
    """Stable identifiers consumed by scoring and API layers."""

    NO_SPONSORSHIP = "NO_SPONSORSHIP"
    ITAR_RESTRICTED = "ITAR_RESTRICTED"
    SECURITY_CLEARANCE = "SECURITY_CLEARANCE"
    DEFENSE_RELATED = "DEFENSE_RELATED"
    COMPANY_TOO_SMALL = "COMPANY_TOO_SMALL"
    BASE_SALARY_TOO_LOW = "BASE_SALARY_TOO_LOW"
    SPONSORSHIP_UNKNOWN = "SPONSORSHIP_UNKNOWN"


class ScannerModel(BaseModel):
    """Strict immutable base model for scanner inputs and outputs."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class JobPostingFacts(ScannerModel):
    """Structured facts available before AI-based job scoring."""

    title: str = Field(min_length=1)
    company: str = Field(min_length=1)
    description: str = Field(min_length=1)
    sponsorship_status: SponsorshipStatus = SponsorshipStatus.UNKNOWN
    company_size: int | None = Field(default=None, ge=0)
    base_salary_min_usd: int | None = Field(default=None, ge=0)
    base_salary_max_usd: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def salary_range_is_ordered(self) -> Self:
        if (
            self.base_salary_min_usd is not None
            and self.base_salary_max_usd is not None
            and self.base_salary_max_usd < self.base_salary_min_usd
        ):
            raise ValueError(
                "base_salary_max_usd cannot be less than base_salary_min_usd."
            )
        return self

    @property
    def searchable_text(self) -> str:
        """Combine text fields for rules that may appear anywhere in a posting."""

        return "\n".join((self.title, self.company, self.description))


class RedFlag(ScannerModel):
    """One explainable rule match."""

    code: RedFlagCode
    severity: FlagSeverity
    message: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class ScanResult(ScannerModel):
    """Hard-filter and review outcomes for one job posting."""

    hard_flags: tuple[RedFlag, ...] = ()
    review_flags: tuple[RedFlag, ...] = ()

    @property
    def rejected(self) -> bool:
        """Return whether at least one documented hard filter matched."""

        return bool(self.hard_flags)

    @property
    def requires_manual_review(self) -> bool:
        """Return whether a non-rejection signal needs human confirmation."""

        return bool(self.review_flags)


_NO_SPONSORSHIP_MESSAGE = (
    "The employer explicitly indicates that sponsorship is unavailable."
)


_TEXT_RULES: tuple[tuple[RedFlagCode, str, tuple[re.Pattern[str], ...]], ...] = (
    (
        RedFlagCode.NO_SPONSORSHIP,
        _NO_SPONSORSHIP_MESSAGE,
        (
            re.compile(
                r"\b(?:no|without)\s+(?:visa\s+|employment\s+)?sponsorship\b",
                re.IGNORECASE,
            ),
            re.compile(
                r"\b(?:cannot|can't|unable\s+to|will\s+not|won't|do\s+not|"
                r"does\s+not)\s+(?:provide|offer)?\s*(?:visa\s+)?sponsorship\b",
                re.IGNORECASE,
            ),
            re.compile(
                r"\bauthori[sz]ed\s+to\s+work\b.{0,80}\bwithout\b.{0,40}"
                r"\bsponsorship\b",
                re.IGNORECASE | re.DOTALL,
            ),
            re.compile(
                r"\bnot\s+(?:eligible|available)\s+for\s+"
                r"(?:(?:visa|employment)\s+)?sponsorship\b",
                re.IGNORECASE,
            ),
        ),
    ),
    (
        RedFlagCode.ITAR_RESTRICTED,
        "The role is subject to ITAR or equivalent export-control restrictions.",
        (
            re.compile(r"\bITAR\b", re.IGNORECASE),
            re.compile(
                r"\bInternational\s+Traffic\s+in\s+Arms\s+Regulations\b",
                re.IGNORECASE,
            ),
        ),
    ),
    (
        RedFlagCode.SECURITY_CLEARANCE,
        "The role requires or expects a government security clearance.",
        (
            re.compile(
                r"(?<!no\s)\bsecurity\s+clearance\b.{0,40}\b"
                r"(?:required|mandatory|preferred|obtain|eligible)\b",
                re.IGNORECASE | re.DOTALL,
            ),
            re.compile(
                r"\b(?:active|current)\b.{0,30}\bsecurity\s+clearance\b",
                re.IGNORECASE | re.DOTALL,
            ),
            re.compile(
                r"\b(?:ability|eligible|eligibility)\s+to\s+obtain\b.{0,40}"
                r"\bsecurity\s+clearance\b",
                re.IGNORECASE | re.DOTALL,
            ),
            re.compile(
                r"\b(?:secret|top\s+secret|TS/SCI)\s+clearance\b",
                re.IGNORECASE,
            ),
        ),
    ),
    (
        RedFlagCode.DEFENSE_RELATED,
        "The posting explicitly describes defense or military work.",
        (
            re.compile(r"\bDepartment\s+of\s+Defense\b", re.IGNORECASE),
            re.compile(r"\bDoD\b", re.IGNORECASE),
            re.compile(r"\bdefen[cs]e\s+contractor\b", re.IGNORECASE),
            re.compile(r"\bdefen[cs]e\s+industry\b", re.IGNORECASE),
            re.compile(
                r"\baerospace\s+(?:and|&)\s+defen[cs]e\b",
                re.IGNORECASE,
            ),
            re.compile(
                r"\bmilitary\s+(?:weapon|weapons|system|systems|program|"
                r"programs|platform|platforms)\b",
                re.IGNORECASE,
            ),
        ),
    ),
)


class RedFlagScanner:
    """Apply documented hard filters without making probabilistic guesses."""

    def __init__(
        self,
        *,
        minimum_base_salary_usd: int = DEFAULT_MINIMUM_BASE_SALARY_USD,
        minimum_company_size: int = DEFAULT_MINIMUM_COMPANY_SIZE,
    ) -> None:
        if minimum_base_salary_usd < 0:
            raise ValueError("minimum_base_salary_usd cannot be negative.")
        if minimum_company_size < 1:
            raise ValueError("minimum_company_size must be at least 1.")
        self.minimum_base_salary_usd = minimum_base_salary_usd
        self.minimum_company_size = minimum_company_size

    def scan(self, posting: JobPostingFacts) -> ScanResult:
        """Return stable, explainable flags in documented policy order."""

        hard_flags: list[RedFlag] = []
        no_sponsorship_found = False

        if posting.sponsorship_status is SponsorshipStatus.NO:
            hard_flags.append(
                _hard_flag(
                    RedFlagCode.NO_SPONSORSHIP,
                    _NO_SPONSORSHIP_MESSAGE,
                    "sponsorship_status=NO",
                )
            )
            no_sponsorship_found = True

        for code, message, patterns in _TEXT_RULES:
            if code is RedFlagCode.NO_SPONSORSHIP and no_sponsorship_found:
                continue
            match = _first_match(patterns, posting.searchable_text)
            if match is None:
                continue
            evidence = _evidence_excerpt(posting.searchable_text, match)
            hard_flags.append(_hard_flag(code, message, evidence))
            if code is RedFlagCode.NO_SPONSORSHIP:
                no_sponsorship_found = True

        if (
            posting.company_size is not None
            and posting.company_size < self.minimum_company_size
        ):
            hard_flags.append(
                _hard_flag(
                    RedFlagCode.COMPANY_TOO_SMALL,
                    "The known company size is below the configured minimum.",
                    f"company_size={posting.company_size}; "
                    f"minimum={self.minimum_company_size}",
                )
            )

        if (
            posting.base_salary_max_usd is not None
            and posting.base_salary_max_usd < self.minimum_base_salary_usd
        ):
            hard_flags.append(
                _hard_flag(
                    RedFlagCode.BASE_SALARY_TOO_LOW,
                    "The known maximum base salary is below the configured minimum.",
                    f"base_salary_max_usd={posting.base_salary_max_usd}; "
                    f"minimum={self.minimum_base_salary_usd}",
                )
            )

        review_flags: tuple[RedFlag, ...] = ()
        if (
            posting.sponsorship_status is SponsorshipStatus.UNKNOWN
            and not no_sponsorship_found
        ):
            review_flags = (
                RedFlag(
                    code=RedFlagCode.SPONSORSHIP_UNKNOWN,
                    severity=FlagSeverity.MANUAL_REVIEW,
                    message="Sponsorship availability requires manual confirmation.",
                    evidence="sponsorship_status=UNKNOWN",
                ),
            )

        return ScanResult(
            hard_flags=tuple(hard_flags),
            review_flags=review_flags,
        )


def _hard_flag(code: RedFlagCode, message: str, evidence: str) -> RedFlag:
    return RedFlag(
        code=code,
        severity=FlagSeverity.HARD_FILTER,
        message=message,
        evidence=evidence,
    )


def _first_match(
    patterns: tuple[re.Pattern[str], ...],
    text: str,
) -> re.Match[str] | None:
    for pattern in patterns:
        if match := pattern.search(text):
            return match
    return None


def _evidence_excerpt(text: str, match: re.Match[str], radius: int = 60) -> str:
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    excerpt = " ".join(text[start:end].split())
    if start:
        excerpt = "..." + excerpt
    if end < len(text):
        excerpt += "..."
    return excerpt
