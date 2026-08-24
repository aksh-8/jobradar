"""Tests for provider-independent scoring and fallback behavior."""

import asyncio
import json

import httpx
import pytest

from backend.red_flag_scanner import JobPostingFacts, RedFlagScanner, SponsorshipStatus
from backend.resume_store import ResumeProfile, ResumeStatus, Skill
from backend.scoring_engine import (
    AllScoringProvidersFailedError,
    GeminiScoringProvider,
    OllamaScoringProvider,
    ProviderAssessment,
    ScoreDimensions,
    ScoreVerdict,
    ApplicationVerdict,
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
            skills_match=score,
            experience_level=score,
            domain_relevance=score,
            role_type=score,
            compensation_signal=score,
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
            skills_match=70,
            experience_level=60,
            domain_relevance=80,
            role_type=50,
            compensation_signal=80,
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
async def test_known_sponsor_and_unknown_salary_can_still_be_apply_referral() -> None:
    primary = StubProvider("primary", make_assessment(90))
    engine = ScoringEngine(
        primary,
        scanner=RedFlagScanner(known_sponsor_companies=("Apple",)),
    )

    result = await engine.score(
        make_posting(
            company="Apple",
            sponsorship_status=SponsorshipStatus.UNKNOWN,
            base_salary_min_usd=None,
            base_salary_max_usd=None,
        ),
        make_resume(),
    )

    assert result.verdict is ScoreVerdict.QUALIFIED
    assert result.action_verdict is ApplicationVerdict.APPLY_REFERRAL
    assert result.review_flags


@pytest.mark.asyncio
async def test_unknown_salary_uses_policy_owned_neutral_compensation_score() -> None:
    assessment = ProviderAssessment(
        dimensions=ScoreDimensions(
            skills_match=80,
            experience_level=80,
            domain_relevance=80,
            role_type=80,
            compensation_signal=0,
        ),
        rationale=("Salary was not supplied.",),
    )
    result = await ScoringEngine(StubProvider("primary", assessment)).score(
        make_posting(base_salary_min_usd=None, base_salary_max_usd=None),
        make_resume(),
    )

    assert result.dimensions is not None
    assert result.dimensions.compensation_signal == 50
    assert result.overall_score == 77


@pytest.mark.asyncio
async def test_known_salary_preserves_provider_compensation_score() -> None:
    assessment = make_assessment(80).model_copy(
        update={
            "dimensions": make_assessment(80).dimensions.model_copy(
                update={"compensation_signal": 95}
            )
        }
    )
    result = await ScoringEngine(StubProvider("primary", assessment)).score(
        make_posting(), make_resume()
    )

    assert result.dimensions is not None
    assert result.dimensions.compensation_signal == 95


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
async def test_gemini_sdk_uses_deterministic_generation_config(monkeypatch) -> None:
    from google import genai

    captured: dict[str, object] = {}

    class FakeResponse:
        text = make_assessment().model_dump_json()

    class FakeModels:
        async def generate_content(self, *, model, contents, config):
            captured["model"] = model
            captured["prompt"] = contents
            captured["generation_config"] = config
            return FakeResponse()

    class FakeAsyncClient:
        models = FakeModels()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return None

    class FakeClient:
        def __init__(self, *, api_key):
            captured["api_key"] = api_key
            self.aio = FakeAsyncClient()

    monkeypatch.setattr(genai, "Client", FakeClient)

    result = await GeminiScoringProvider(api_key="test-key").score(
        make_posting(), make_resume()
    )

    assert result == make_assessment()
    config = captured["generation_config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == ProviderAssessment.model_json_schema()
    assert config.temperature == 0
    assert config.automatic_function_calling.disable is True


@pytest.mark.asyncio
async def test_gemini_adapter_rejects_invalid_output() -> None:
    async def generate(prompt: str) -> str:
        return '{"dimensions": {"skills_match": 101}}'

    with pytest.raises(ScoringProviderError, match="Gemini scoring failed"):
        await GeminiScoringProvider(generate=generate).score(
            make_posting(), make_resume()
        )


@pytest.mark.asyncio
async def test_gemini_adapter_enforces_request_timeout() -> None:
    async def generate(prompt: str) -> str:
        await asyncio.sleep(0.05)
        return make_assessment().model_dump_json()

    with pytest.raises(ScoringProviderError, match="Gemini scoring failed"):
        await GeminiScoringProvider(
            generate=generate,
            timeout_seconds=0.01,
        ).score(make_posting(), make_resume())


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
