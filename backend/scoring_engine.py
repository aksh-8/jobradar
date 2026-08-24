"""Structured job scoring with Gemini as primary and Ollama as fallback.

The orchestration layer is provider-independent: deterministic hard filters run
first, provider output is strictly validated, and a failed primary provider is
retried once through the configured local fallback.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.red_flag_scanner import (
    JobPostingFacts,
    RedFlag,
    RedFlagCode,
    RedFlagScanner,
    SponsorshipStatus,
)
from backend.resume_store import ResumeProfile, ResumeStatus

DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"
DEFAULT_MINIMUM_OVERALL_SCORE = 60
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
NEUTRAL_COMPENSATION_SCORE = 50


class ScoringModel(BaseModel):
    """Strict immutable base model for scoring contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class ScoreDimensions(ScoringModel):
    """Auditable fit scores returned by a scoring provider."""

    skills_match: int = Field(ge=0, le=100)
    experience_level: int = Field(ge=0, le=100)
    domain_relevance: int = Field(ge=0, le=100)
    role_type: int = Field(ge=0, le=100)
    compensation_signal: int = Field(ge=0, le=100)

    @property
    def weighted_overall(self) -> int:
        """Compute the policy-owned score instead of trusting an AI total."""

        value = (
            self.skills_match * 0.35
            + self.experience_level * 0.25
            + self.domain_relevance * 0.20
            + self.role_type * 0.10
            + self.compensation_signal * 0.10
        )
        return round(value)


class ProviderAssessment(ScoringModel):
    """The only JSON shape accepted from Gemini or Ollama."""

    dimensions: ScoreDimensions
    matched_requirements: tuple[str, ...] = ()
    missing_requirements: tuple[str, ...] = ()
    rationale: tuple[str, ...] = Field(min_length=1, max_length=5)


class ScoreVerdict(str, Enum):
    """Stable decision consumed by API, extension, and discovery clients."""

    REJECTED = "REJECTED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    QUALIFIED = "QUALIFIED"
    BELOW_THRESHOLD = "BELOW_THRESHOLD"


class ApplicationVerdict(str, Enum):
    """User-facing action that follows JobRadar's application strategy."""

    APPLY_REFERRAL = "APPLY+REFERRAL"
    BORDERLINE = "BORDERLINE"
    SKIP = "SKIP"


class ScoringResult(ScoringModel):
    """Complete scoring outcome, including deterministic policy signals."""

    overall_score: int = Field(ge=0, le=100)
    verdict: ScoreVerdict
    action_verdict: ApplicationVerdict | None = None
    resume_profile_id: str | None = None
    recommended_resume: str | None = None
    sponsorship_signal: SponsorshipStatus | None = None
    base_salary_min_usd: int | None = Field(default=None, ge=0)
    base_salary_max_usd: int | None = Field(default=None, ge=0)
    provider: str | None = None
    dimensions: ScoreDimensions | None = None
    matched_requirements: tuple[str, ...] = ()
    missing_requirements: tuple[str, ...] = ()
    rationale: tuple[str, ...] = ()
    hard_flags: tuple[RedFlag, ...] = ()
    review_flags: tuple[RedFlag, ...] = ()
    fallback_used: bool = False


class ScoringProviderError(RuntimeError):
    """Raised when one scoring provider cannot return a valid assessment."""


class AllScoringProvidersFailedError(ScoringProviderError):
    """Raised when both primary and fallback providers fail."""


class ScoringProvider(Protocol):
    """Interface implemented by remote and local structured scorers."""

    name: str

    async def score(
        self, posting: JobPostingFacts, resume: ResumeProfile
    ) -> ProviderAssessment: ...


class GeminiScoringProvider:
    """Gemini structured-output adapter with lazy SDK initialization."""

    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        generate: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model = model or os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        configured_timeout = os.getenv("REQUEST_TIMEOUT_SECONDS")
        self.timeout_seconds = timeout_seconds or (
            float(configured_timeout)
            if configured_timeout
            else DEFAULT_REQUEST_TIMEOUT_SECONDS
        )
        self._generate = generate

    async def score(
        self, posting: JobPostingFacts, resume: ResumeProfile
    ) -> ProviderAssessment:
        prompt = _build_prompt(posting, resume)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                raw = (
                    await self._generate(prompt)
                    if self._generate is not None
                    else await self._generate_with_sdk(prompt)
                )
            return _parse_assessment(raw)
        except ScoringProviderError:
            raise
        except (
            TimeoutError,
            ValidationError,
            ValueError,
            TypeError,
            RuntimeError,
        ) as error:
            raise ScoringProviderError(f"Gemini scoring failed: {error}") from error

    async def _generate_with_sdk(self, prompt: str) -> str:
        if not self.api_key or self.api_key.startswith("replace_with_"):
            raise ScoringProviderError("GEMINI_API_KEY is not configured.")

        # Imported lazily so deterministic tests and Ollama-only use do not
        # initialize the Gemini SDK or require credentials.
        from google import genai
        from google.genai import types

        async with genai.Client(api_key=self.api_key).aio as client:
            response = await client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                    response_mime_type="application/json",
                    response_json_schema=ProviderAssessment.model_json_schema(),
                    temperature=0,
                ),
            )
        text = getattr(response, "text", None)
        if not text:
            raise ScoringProviderError("Gemini returned no text response.")
        return str(text)


class OllamaScoringProvider:
    """Local Ollama chat adapter using JSON-schema constrained output."""

    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv(
            "OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL
        )).rstrip("/")
        self.model = model or os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
        configured_timeout = os.getenv("REQUEST_TIMEOUT_SECONDS")
        self.timeout_seconds = timeout_seconds or (
            float(configured_timeout)
            if configured_timeout
            else DEFAULT_REQUEST_TIMEOUT_SECONDS
        )
        self.transport = transport

    async def score(
        self, posting: JobPostingFacts, resume: ResumeProfile
    ) -> ProviderAssessment:
        payload = {
            "model": self.model,
            "stream": False,
            "format": ProviderAssessment.model_json_schema(),
            "messages": [{"role": "user", "content": _build_prompt(posting, resume)}],
            "options": {"temperature": 0},
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=payload)
                response.raise_for_status()
                body = response.json()
            content = body["message"]["content"]
            return _parse_assessment(content)
        except (httpx.HTTPError, KeyError, json.JSONDecodeError, ValidationError,
                ValueError, TypeError) as error:
            raise ScoringProviderError(f"Ollama scoring failed: {error}") from error


class ScoringEngine:
    """Apply hard filters and coordinate primary/fallback AI scoring."""

    def __init__(
        self,
        primary: ScoringProvider,
        fallback: ScoringProvider | None = None,
        *,
        scanner: RedFlagScanner | None = None,
        minimum_overall_score: int | None = None,
    ) -> None:
        threshold = minimum_overall_score
        if threshold is None:
            threshold = int(os.getenv(
                "MINIMUM_OVERALL_SCORE", DEFAULT_MINIMUM_OVERALL_SCORE
            ))
        if not 0 <= threshold <= 100:
            raise ValueError("minimum_overall_score must be between 0 and 100.")
        self.primary = primary
        self.fallback = fallback
        self.scanner = scanner or RedFlagScanner()
        self.minimum_overall_score = threshold

    async def score(
        self, posting: JobPostingFacts, resume: ResumeProfile
    ) -> ScoringResult:
        if resume.status is not ResumeStatus.READY:
            raise ValueError("A ready resume profile is required for scoring.")

        scan = self.scanner.scan(posting)
        if scan.rejected:
            return ScoringResult(
                overall_score=0,
                verdict=ScoreVerdict.REJECTED,
                action_verdict=ApplicationVerdict.SKIP,
                resume_profile_id=resume.profile_id,
                recommended_resume=resume.resume_name or resume.profile_id,
                sponsorship_signal=posting.sponsorship_status,
                base_salary_min_usd=posting.base_salary_min_usd,
                base_salary_max_usd=posting.base_salary_max_usd,
                rationale=("Rejected by deterministic hard-filter policy.",),
                hard_flags=scan.hard_flags,
                review_flags=scan.review_flags,
            )

        assessment, provider_name, fallback_used = await self._provider_score(
            posting, resume
        )
        assessment = _apply_policy_owned_defaults(posting, assessment)
        overall = assessment.dimensions.weighted_overall
        blocking_review = any(
            flag.code is RedFlagCode.SPONSORSHIP_UNKNOWN
            for flag in scan.review_flags
        )
        if blocking_review:
            verdict = ScoreVerdict.MANUAL_REVIEW
            action_verdict = ApplicationVerdict.BORDERLINE
        elif overall >= self.minimum_overall_score:
            verdict = ScoreVerdict.QUALIFIED
            action_verdict = ApplicationVerdict.APPLY_REFERRAL
        else:
            verdict = ScoreVerdict.BELOW_THRESHOLD
            action_verdict = ApplicationVerdict.SKIP

        return ScoringResult(
            overall_score=overall,
            verdict=verdict,
            action_verdict=action_verdict,
            resume_profile_id=resume.profile_id,
            recommended_resume=resume.resume_name or resume.profile_id,
            sponsorship_signal=posting.sponsorship_status,
            base_salary_min_usd=posting.base_salary_min_usd,
            base_salary_max_usd=posting.base_salary_max_usd,
            provider=provider_name,
            dimensions=assessment.dimensions,
            matched_requirements=assessment.matched_requirements,
            missing_requirements=assessment.missing_requirements,
            rationale=assessment.rationale,
            hard_flags=scan.hard_flags,
            review_flags=scan.review_flags,
            fallback_used=fallback_used,
        )

    async def _provider_score(
        self, posting: JobPostingFacts, resume: ResumeProfile
    ) -> tuple[ProviderAssessment, str, bool]:
        try:
            return await self.primary.score(posting, resume), self.primary.name, False
        except Exception as primary_error:
            if self.fallback is None:
                raise AllScoringProvidersFailedError(
                    f"Primary provider {self.primary.name!r} failed: {primary_error}"
                ) from primary_error
            try:
                assessment = await self.fallback.score(posting, resume)
                return assessment, self.fallback.name, True
            except Exception as fallback_error:
                raise AllScoringProvidersFailedError(
                    f"Primary provider {self.primary.name!r} failed: {primary_error}; "
                    f"fallback provider {self.fallback.name!r} failed: {fallback_error}"
                ) from fallback_error


def create_default_scoring_engine() -> ScoringEngine:
    """Create the production Gemini-first, Ollama-fallback engine."""

    return ScoringEngine(
        primary=GeminiScoringProvider(),
        fallback=OllamaScoringProvider(),
    )


def _build_prompt(posting: JobPostingFacts, resume: ResumeProfile) -> str:
    payload: dict[str, Any] = {
        "job": posting.model_dump(mode="json"),
        "resume": resume.scoring_context(),
    }
    return (
        "Score this job against only the verified resume facts supplied. "
        "Do not infer missing experience or credentials. Score each dimension "
        "from 0 to 100 using these policy meanings: skills_match is hard-skill "
        "fit, experience_level compares required experience with verified history, "
        "domain_relevance measures platform/automation/AI context, role_type favors "
        "builders and framework owners over manual executors, and "
        "compensation_signal measures stated compensation against the configured "
        "minimum and preference. When no salary range is supplied, return the "
        f"neutral compensation score {NEUTRAL_COMPENSATION_SCORE}; application code "
        "also enforces this default. Keep rationale concise and return JSON only.\n"
        f"Input:\n{json.dumps(payload, sort_keys=True)}"
    )


def _parse_assessment(raw: str) -> ProviderAssessment:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])
            if text.lstrip().startswith("json"):
                text = text.lstrip()[4:].lstrip()
    return ProviderAssessment.model_validate_json(text)


def _apply_policy_owned_defaults(
    posting: JobPostingFacts,
    assessment: ProviderAssessment,
) -> ProviderAssessment:
    """Remove model discretion where the input contains no compensation signal."""

    if (
        posting.base_salary_min_usd is not None
        or posting.base_salary_max_usd is not None
    ):
        return assessment
    dimensions = assessment.dimensions.model_copy(
        update={"compensation_signal": NEUTRAL_COMPENSATION_SCORE}
    )
    return assessment.model_copy(update={"dimensions": dimensions})
