"""Grounded cover-letter prompt and output policy tests."""

import pytest

from agent.cover_letter import (
    CoverLetterGenerationError,
    GeminiCoverLetterGenerator,
    SYSTEM_PROMPT,
)
from tests.test_outreach import fde_profile, scale_posting
from backend.scoring_engine import ScoreVerdict, ScoringResult


COMPLIANT_LETTER = """Scale AI's evaluation platform addresses a concrete production challenge. The Forward Deployed Engineer role connects that work directly with customer teams.

Platform engineering work at Porsche Engineering US required owning automation from design through delivery. LLM integrations built with Gemini and Qwen added practical experience connecting model behavior to usable tools. That combination maps well to production GenAI systems.

Curiosity drives fast investigation, while ownership keeps delivery moving after the first prototype. Clear communication keeps technical and product decisions aligned.

Could we discuss the team's current priorities?"""


def score() -> ScoringResult:
    return ScoringResult(
        overall_score=88,
        verdict=ScoreVerdict.QUALIFIED,
        resume_profile_id="akash-biswal-fde",
        recommended_resume="FDE",
        skills_matched=("platform engineering", "LLM integrations"),
    )


@pytest.mark.asyncio
async def test_cover_letter_prompt_uses_verified_role_skills_and_resume() -> None:
    captured = {}

    async def generate(system_prompt: str, user_prompt: str) -> str:
        captured.update(system=system_prompt, user=user_prompt)
        return COMPLIANT_LETTER

    letter = await GeminiCoverLetterGenerator(generate=generate).generate(
        scale_posting(), score(), fde_profile()
    )

    assert letter == COMPLIANT_LETTER
    assert captured["system"] == SYSTEM_PROMPT
    assert "Software Engineer, Test Automation & Platform at Porsche Engineering US" in captured["user"]
    assert "platform engineering, LLM integrations" in captured["user"]
    assert "Resume variant being used: FDE" in captured["user"]
    assert "At least 2 years" in captured["user"]


@pytest.mark.asyncio
async def test_cover_letter_retries_a_sentence_that_starts_with_i() -> None:
    outputs = iter(("I am excited to apply.", COMPLIANT_LETTER))
    calls = 0

    async def generate(_system_prompt: str, _user_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return next(outputs)

    letter = await GeminiCoverLetterGenerator(generate=generate).generate(
        scale_posting(), score(), fde_profile()
    )

    assert calls == 2
    assert letter == COMPLIANT_LETTER


@pytest.mark.asyncio
async def test_cover_letter_converts_provider_failure_to_domain_error() -> None:
    async def generate(_system_prompt: str, _user_prompt: str) -> str:
        raise RuntimeError("404 model unavailable")

    generator = GeminiCoverLetterGenerator(
        generate=generate,
        model="gemini-3.6-flash",
    )

    with pytest.raises(CoverLetterGenerationError) as raised:
        await generator.generate(scale_posting(), score(), fde_profile())

    assert "gemini-3.6-flash" in str(raised.value)
    assert "404 model unavailable" in str(raised.value)
