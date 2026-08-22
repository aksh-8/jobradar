"""Tests for provider-independent scoring and fallback behavior."""

import json

import httpx
import pytest

from backend.red_flag_scanner import JobPostingFacts, SponsorshipStatus
from backend.resume_store import ResumeProfile, ResumeStatus, Skill
from backend.scoring_engine import (
    AllScoringProvidersFailedError,
    GeminiScoringProvider,
    OllamaScoringProvider,
    ProviderAssessment,
    ScoreDimensions,
    ScoreVerdict,
    ScoringEngine,
    ScoringProviderError,
)


def make_resume() -> ResumeProfile:
    return ResumeProfile(
        profile_id="backend",
        full_name="Test Candidate",
        status=ResumeStatus.READY,
        summary="Builds Python services.",
        target_roles=("Backend Engineer",),
        skills=(Skill(name="Python", category="Languages"),),
    )


def make_posting(**overrides: object) -> JobPostingFacts:
    values: dict[str, object] = {
        "title": "Backend Engineer",
        "company": "Acme",
        "description": "Build Python services.",
        "sponsorship_status": SponsorshipStatus.YES,
        "company_size": 500,
        "base_salary_max_usd": 180_000,
    }
    values.update(overrides)
    return JobPostingFacts.model_validate(values)


def make_assessment(score: int = 80) -> ProviderAssessment:
    return ProviderAssessment(
        dimensions=ScoreDimensions(
            role_alignment=score,
            required_skills=score,
            experience_fit=score,
            career_fit=score,
        ),
        matched_requirements=("Python",),
        rationale=("Verified Python experience matches the role.",),
    )


class StubProvider:
    def __init__(self, name: str, result: ProviderAssessment | Exception) -> None:
        self.name = name
        self.result = result
        self.calls = 0

    async def score(self, posting, resume) -> ProviderAssessment:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.asyncio
async def test_hard_filter_short_circuits_all_ai_providers() -> None:
    primary = StubProvider("primary", make_assessment())
    fallback = StubProvider("fallback", make_assessment())
    engine = ScoringEngine(primary, fallback)

    result = await engine.score(
        make_posting(description="No visa sponsorship is available."), make_resume()
    )

    assert result.verdict is ScoreVerdict.REJECTED
    assert result.overall_score == 0
    assert result.provider is None
    assert primary.calls == fallback.calls == 0


@pytest.mark.asyncio
async def test_primary_assessment_uses_policy_weighting_and_threshold() -> None:
    assessment = ProviderAssessment(
        dimensions=ScoreDimensions(
            role_alignment=80,
            required_skills=70,
            experience_fit=60,
            career_fit=50,
        ),
        rationale=("Evidence-based result.",),
    )
    primary = StubProvider("primary", assessment)

    result = await ScoringEngine(primary, minimum_overall_score=70).score(
        make_posting(), make_resume()
    )

    assert result.overall_score == 68
    assert result.verdict is ScoreVerdict.BELOW_THRESHOLD
    assert result.provider == "primary"
    assert result.fallback_used is False


@pytest.mark.asyncio
async def test_unknown_sponsorship_preserves_score_but_requires_review() -> None:
    primary = StubProvider("primary", make_assessment(90))

    result = await ScoringEngine(primary).score(
        make_posting(sponsorship_status=SponsorshipStatus.UNKNOWN), make_resume()
    )

    assert result.overall_score == 90
    assert result.verdict is ScoreVerdict.MANUAL_REVIEW
    assert result.review_flags


@pytest.mark.asyncio
async def test_fallback_is_used_after_primary_failure() -> None:
    primary = StubProvider("gemini", ScoringProviderError("unavailable"))
    fallback = StubProvider("ollama", make_assessment(75))

    result = await ScoringEngine(primary, fallback).score(
        make_posting(), make_resume()
    )

    assert result.provider == "ollama"
    assert result.fallback_used is True
    assert primary.calls == fallback.calls == 1


@pytest.mark.asyncio
async def test_both_provider_failures_are_reported() -> None:
    engine = ScoringEngine(
        StubProvider("gemini", RuntimeError("remote down")),
        StubProvider("ollama", RuntimeError("local down")),
    )

    with pytest.raises(AllScoringProvidersFailedError, match="remote down.*local down"):
        await engine.score(make_posting(), make_resume())


@pytest.mark.asyncio
async def test_draft_resume_is_rejected_before_provider_call() -> None:
    primary = StubProvider("primary", make_assessment())
    draft = ResumeProfile(profile_id="draft", full_name="Test Candidate")

    with pytest.raises(ValueError, match="ready resume"):
        await ScoringEngine(primary).score(make_posting(), draft)
    assert primary.calls == 0


@pytest.mark.asyncio
async def test_gemini_adapter_validates_structured_json() -> None:
    async def generate(prompt: str) -> str:
        assert "verified resume facts" in prompt
        return make_assessment().model_dump_json()

    result = await GeminiScoringProvider(generate=generate).score(
        make_posting(), make_resume()
    )

    assert result.dimensions.weighted_overall == 80


@pytest.mark.asyncio
async def test_gemini_adapter_rejects_invalid_output() -> None:
    async def generate(prompt: str) -> str:
        return '{"dimensions": {"role_alignment": 101}}'

    with pytest.raises(ScoringProviderError, match="Gemini scoring failed"):
        await GeminiScoringProvider(generate=generate).score(
            make_posting(), make_resume()
        )


@pytest.mark.asyncio
async def test_ollama_adapter_sends_schema_and_parses_response() -> None:
    assessment = make_assessment(85)

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/api/chat"
        assert body["model"] == "test-model"
        assert body["format"]["title"] == "ProviderAssessment"
        return httpx.Response(
            200,
            json={"message": {"content": assessment.model_dump_json()}},
        )

    provider = OllamaScoringProvider(
        base_url="http://ollama.test",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    result = await provider.score(make_posting(), make_resume())

    assert result == assessment
