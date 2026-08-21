"""Validated, structured resume profiles used by JobRadar scoring.

The catalog intentionally stores structured facts rather than parsing private
resume documents at runtime.  Only facts explicitly supplied to a profile are
made available to downstream scoring code.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResumeStatus(str, Enum):
    """Whether a profile has enough verified content for job scoring."""

    DRAFT = "draft"
    READY = "ready"


class ResumeModel(BaseModel):
    """Strict immutable base model for resume catalog records."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class Skill(ResumeModel):
    """A skill and the terms by which job descriptions may refer to it."""

    name: str = Field(min_length=1)
    category: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    years_experience: float | None = Field(default=None, ge=0, le=80)

    @model_validator(mode="after")
    def aliases_are_unique(self) -> Self:
        aliases = [_normalized_phrase(alias) for alias in self.aliases]
        if any(not alias for alias in aliases):
            raise ValueError("Skill aliases cannot be blank.")
        if len(aliases) != len(set(aliases)):
            raise ValueError("Skill aliases must be unique.")
        if _normalized_phrase(self.name) in aliases:
            raise ValueError("A skill alias cannot repeat the skill name.")
        return self


class WorkExperience(ResumeModel):
    """One verified employment entry."""

    company: str = Field(min_length=1)
    title: str = Field(min_length=1)
    start_date: date
    end_date: date | None = None
    location: str | None = None
    summary: str | None = None
    highlights: tuple[str, ...] = ()
    skill_names: tuple[str, ...] = ()

    @model_validator(mode="after")
    def dates_are_ordered(self) -> Self:
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("Work experience end_date cannot precede start_date.")
        return self


class Education(ResumeModel):
    """One education credential."""

    institution: str = Field(min_length=1)
    degree: str = Field(min_length=1)
    field_of_study: str | None = None
    graduation_date: date | None = None


class Certification(ResumeModel):
    """One professional certification."""

    name: str = Field(min_length=1)
    issuer: str = Field(min_length=1)
    issued_date: date | None = None
    expires_date: date | None = None

    @model_validator(mode="after")
    def dates_are_ordered(self) -> Self:
        if (
            self.issued_date is not None
            and self.expires_date is not None
            and self.expires_date < self.issued_date
        ):
            raise ValueError("Certification expires_date cannot precede issued_date.")
        return self


class ResumeProfile(ResumeModel):
    """A role-targeted collection of verified resume facts."""

    profile_id: str = Field(
        min_length=1,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    full_name: str = Field(min_length=1)
    status: ResumeStatus = ResumeStatus.DRAFT
    headline: str | None = None
    summary: str | None = None
    target_roles: tuple[str, ...] = ()
    skills: tuple[Skill, ...] = ()
    experience: tuple[WorkExperience, ...] = ()
    education: tuple[Education, ...] = ()
    certifications: tuple[Certification, ...] = ()
    keywords: tuple[str, ...] = ()

    @model_validator(mode="after")
    def content_is_consistent(self) -> Self:
        _require_unique("target roles", self.target_roles)
        _require_unique("keywords", self.keywords)
        _require_unique("skill names", tuple(skill.name for skill in self.skills))

        known_skills = {
            _normalized_phrase(skill.name) for skill in self.skills
        }
        for entry in self.experience:
            unknown_skills = {
                skill_name
                for skill_name in entry.skill_names
                if _normalized_phrase(skill_name) not in known_skills
            }
            if unknown_skills:
                unknown_list = ", ".join(sorted(unknown_skills))
                raise ValueError(
                    f"Work experience references unknown skills: {unknown_list}."
                )

        if self.status is ResumeStatus.READY:
            missing = []
            if not self.summary:
                missing.append("summary")
            if not self.target_roles:
                missing.append("target_roles")
            if not self.skills:
                missing.append("skills")
            if missing:
                raise ValueError(
                    "Ready resume profiles require: " + ", ".join(missing) + "."
                )
        return self

    def scoring_context(self) -> dict[str, object]:
        """Return a JSON-ready view containing only job-scoring facts."""

        return self.model_dump(
            mode="json",
            exclude={"status"},
            exclude_none=True,
        )


class DuplicateResumeProfileError(ValueError):
    """Raised when a catalog receives the same profile ID more than once."""


class ResumeProfileNotFoundError(LookupError):
    """Raised when a requested resume profile is not in the catalog."""


@dataclass(frozen=True)
class ResumeMatch:
    """A deterministic match between a job description and a profile."""

    profile: ResumeProfile
    score: int
    matched_terms: tuple[str, ...]


class ResumeCatalog:
    """An immutable collection of validated, role-targeted resume profiles."""

    def __init__(self, profiles: Iterable[ResumeProfile] = ()) -> None:
        indexed_profiles: dict[str, ResumeProfile] = {}
        for profile in profiles:
            if profile.profile_id in indexed_profiles:
                raise DuplicateResumeProfileError(
                    f"Duplicate resume profile ID: {profile.profile_id!r}."
                )
            indexed_profiles[profile.profile_id] = profile
        self._profiles = indexed_profiles

    def list_profiles(self, *, ready_only: bool = False) -> tuple[ResumeProfile, ...]:
        """List profiles in stable ID order, optionally excluding drafts."""

        profiles = self._profiles.values()
        if ready_only:
            profiles = (
                profile
                for profile in profiles
                if profile.status is ResumeStatus.READY
            )
        return tuple(sorted(profiles, key=lambda profile: profile.profile_id))

    def get(self, profile_id: str) -> ResumeProfile:
        """Return a profile by ID with a useful error for configuration issues."""

        try:
            return self._profiles[profile_id]
        except KeyError as error:
            available = ", ".join(sorted(self._profiles)) or "none"
            raise ResumeProfileNotFoundError(
                f"Unknown resume profile {profile_id!r}; available profiles: {available}."
            ) from error

    def rank_for_job(
        self,
        job_text: str,
        *,
        include_drafts: bool = False,
    ) -> tuple[ResumeMatch, ...]:
        """Rank profiles by explicit target-role, skill, and keyword matches."""

        normalized_job = _normalized_phrase(job_text)
        if not normalized_job:
            raise ValueError("job_text cannot be blank.")

        profiles = self.list_profiles(ready_only=not include_drafts)
        matches = []
        for profile in profiles:
            weighted_terms = _profile_match_terms(profile)
            matched = tuple(
                sorted(term for term in weighted_terms if term in normalized_job)
            )
            matched_groups: dict[str, int] = {}
            for term in matched:
                weight, group = weighted_terms[term]
                matched_groups[group] = max(weight, matched_groups.get(group, 0))
            matches.append(
                ResumeMatch(
                    profile=profile,
                    score=sum(matched_groups.values()),
                    matched_terms=matched,
                )
            )
        return tuple(
            sorted(
                matches,
                key=lambda match: (-match.score, match.profile.profile_id),
            )
        )


def _normalized_phrase(value: str) -> str:
    """Normalize user-entered terms for comparisons without changing stored data."""

    return " ".join(re.sub(r"[^a-z0-9+#.]+", " ", value.casefold()).split())


def _require_unique(label: str, values: tuple[str, ...]) -> None:
    normalized = [_normalized_phrase(value) for value in values]
    if any(not value for value in normalized):
        raise ValueError(f"Resume {label} cannot contain blank values.")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"Resume {label} must be unique.")


def _profile_match_terms(profile: ResumeProfile) -> dict[str, tuple[int, str]]:
    weighted_terms: dict[str, tuple[int, str]] = {}

    def add(value: str, weight: int, group: str) -> None:
        term = _normalized_phrase(value)
        if not term:
            return
        existing = weighted_terms.get(term)
        if existing is None or weight > existing[0]:
            weighted_terms[term] = (weight, group)

    for role in profile.target_roles:
        normalized_role = _normalized_phrase(role)
        add(role, 5, f'role:{normalized_role}')
    for skill in profile.skills:
        normalized_skill = _normalized_phrase(skill.name)
        group = f'skill:{normalized_skill}'
        add(skill.name, 3, group)
        for alias in skill.aliases:
            add(alias, 3, group)
    for keyword in profile.keywords:
        normalized_keyword = _normalized_phrase(keyword)
        add(keyword, 1, f'keyword:{normalized_keyword}')
    return weighted_terms


DEFAULT_PROFILE_ID = "akash-biswal"

# The repository contains no verified career history yet.  Keeping this profile
# in draft state makes that absence explicit and prevents downstream matching
# from treating invented or incomplete credentials as scoring facts.
AKASH_BISWAL_DRAFT = ResumeProfile(
    profile_id=DEFAULT_PROFILE_ID,
    full_name="Akash Biswal",
)

RESUME_CATALOG = ResumeCatalog((AKASH_BISWAL_DRAFT,))


def get_resume_profile(profile_id: str = DEFAULT_PROFILE_ID) -> ResumeProfile:
    """Retrieve a profile from JobRadar's application-wide catalog."""

    return RESUME_CATALOG.get(profile_id)
