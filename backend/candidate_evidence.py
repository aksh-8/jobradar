"""Candidate-owned capability labels derived only from verified resume facts."""

from __future__ import annotations

import re

from backend.red_flag_scanner import JobPostingFacts
from backend.resume_store import ResumeProfile


_SEMANTIC_CAPABILITIES = (
    (
        "platform engineering",
        ("platform", "infrastructure", "developer platform", "developer experience"),
        ("platform", "infrastructure", "developer experience"),
    ),
    (
        "LLM integrations",
        (
            "llm",
            "large language model",
            "generative ai",
            "genai",
            "gemini",
            "qwen",
            "langchain",
            "ai agent",
        ),
        (
            "llm",
            "gemini",
            "qwen",
            "langchain",
            "langgraph",
            "langsmith",
            "ai agent",
        ),
    ),
    (
        "workflow automation",
        ("workflow automation", "automation", "developer tooling"),
        ("workflow automation", "automation", "developer tooling"),
    ),
    (
        "backend engineering",
        ("backend", "api", "microservice", "distributed system"),
        ("backend", "api", "microservice", "distributed system"),
    ),
    (
        "cloud infrastructure",
        ("cloud", "aws", "kubernetes", "terraform", "docker"),
        ("cloud", "aws", "kubernetes", "terraform", "docker"),
    ),
)


def candidate_skills_for_job(
    posting: JobPostingFacts,
    profile: ResumeProfile,
    *,
    limit: int = 3,
) -> tuple[str, ...]:
    """Return job-relevant labels that are all backed by the selected resume."""

    job_text = _normalized(posting.searchable_text)
    profile_text = _profile_text(profile)
    labels: list[str] = []

    for label, job_terms, profile_terms in _SEMANTIC_CAPABILITIES:
        if _contains_any(profile_text, profile_terms) and _contains_any(
            job_text, job_terms
        ):
            _append_unique(labels, label)

    for keyword in profile.keywords:
        if _contains(job_text, keyword):
            _append_unique(labels, keyword)

    for skill in profile.skills:
        terms = (skill.name, *skill.aliases)
        if any(_contains(job_text, term) for term in terms):
            _append_unique(labels, skill.name)

    return tuple(labels[: max(1, limit)])


def fallback_candidate_skills(
    profile: ResumeProfile | None,
    *,
    limit: int = 3,
) -> tuple[str, ...]:
    """Return concise, verified capability labels when no job match was saved."""

    if profile is None:
        return ("platform engineering", "workflow automation")

    profile_text = _profile_text(profile)
    labels: list[str] = []
    for label, _job_terms, profile_terms in _SEMANTIC_CAPABILITIES:
        if _contains_any(profile_text, profile_terms):
            _append_unique(labels, label)
    for keyword in profile.keywords:
        _append_unique(labels, keyword)
    for skill in profile.skills:
        _append_unique(labels, skill.name)
    return tuple(labels[: max(1, limit)])


def _profile_text(profile: ResumeProfile) -> str:
    values: list[str] = [
        profile.headline or "",
        profile.summary or "",
        *profile.target_roles,
        *profile.keywords,
    ]
    for skill in profile.skills:
        values.extend((skill.name, *skill.aliases))
    for experience in profile.experience:
        values.extend(
            (
                experience.title,
                experience.summary or "",
                *experience.highlights,
                *experience.skill_names,
            )
        )
    for project in profile.projects:
        values.extend(
            (project.name, project.summary, *project.highlights, *project.skill_names)
        )
    return _normalized(" ".join(values))


def _normalized(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+#.]+", " ", value.casefold()).split())


def _contains(text: str, phrase: str) -> bool:
    normalized_phrase = _normalized(phrase)
    return bool(
        normalized_phrase
        and f" {normalized_phrase} " in f" {text} "
    )


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(_contains(text, phrase) for phrase in phrases)


def _append_unique(labels: list[str], value: str) -> None:
    normalized_value = _normalized(value)
    if normalized_value and all(_normalized(item) != normalized_value for item in labels):
        labels.append(value)
