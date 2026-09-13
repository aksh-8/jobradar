"""Candidate-grounded outreach regression coverage."""

from datetime import date
import re

import pytest

from agent.outreach import build_outreach
from backend.red_flag_scanner import JobPostingFacts, SponsorshipStatus
from backend.resume_store import ResumeProfile, ResumeStatus, Skill, WorkExperience
from backend.scoring_engine import (
    ProviderAssessment,
    ScoreDimensions,
    ScoreVerdict,
    ScoringEngine,
    ScoringResult,
)


class ScaleAssessmentProvider:
    name = "scale-test"

    async def score(self, posting, resume) -> ProviderAssessment:
        return ProviderAssessment(
            dimensions=ScoreDimensions(
                skills_match=90,
                experience_level=85,
                domain_relevance=92,
                role_type=90,
                compensation_signal=80,
            ),
            matched_requirements=("At least 2 years of relevant experience.",),
            rationale=("Verified candidate capabilities match the work.",),
        )


def fde_profile() -> ResumeProfile:
    return ResumeProfile(
        profile_id="akash-biswal-fde",
        full_name="Akash Biswal",
        resume_name="FDE",
        status=ResumeStatus.READY,
        headline="Forward Deployed Engineer - Platform & Automation",
        summary="Builds production platforms and AI-enabled automation.",
        target_roles=("Forward Deployed Engineer",),
        keywords=("platform engineering", "AI agents", "workflow automation"),
        skills=(
            Skill(name="Python", category="Languages"),
            Skill(name="Gemini API", category="AI", aliases=("Gemini 2.5 Pro",)),
            Skill(name="Qwen", category="AI", aliases=("Qwen 7B",)),
        ),
        experience=(
            WorkExperience(
                company="Porsche Engineering US",
                title="Software Engineer, Test Automation & Platform",
                start_date=date(2024, 1, 1),
                highlights=("Owned a production platform automation workflow.",),
                skill_names=("Python", "Gemini API"),
            ),
        ),
    )


def scale_posting() -> JobPostingFacts:
    return JobPostingFacts(
        title="Forward Deployed Engineer, GenAI",
        company="Scale AI",
        description=(
            "Build GenAI integrations with platform engineering teams and ship "
            "production evaluation systems. At least 2 years of experience required."
        ),
        location="San Francisco, CA, United States",
        sponsorship_status=SponsorshipStatus.YES,
        base_salary_max_usd=220_000,
    )


@pytest.mark.asyncio
async def test_scale_fde_outreach_uses_candidate_skills_not_jd_requirement() -> None:
    profile = fde_profile()
    score = await ScoringEngine(ScaleAssessmentProvider()).score(
        scale_posting(), profile
    )
    guidance = build_outreach(scale_posting(), score, profile)

    assert score.skills_matched[:2] == (
        "platform engineering",
        "LLM integrations",
    )
    for message in (
        guidance.recruiter_message,
        guidance.referral_message,
        guidance.cold_email,
    ):
        assert "platform engineering and LLM integrations" in message
        assert "at least 2 years" not in message.casefold()
        assert 3 <= len(re.findall(r"[.!?](?:\s|$)", message)) <= 5
        assert "hope this finds you well" not in message.casefold()


def test_sponsorship_question_appears_only_for_manual_review() -> None:
    base = ScoringResult(
        overall_score=82,
        verdict=ScoreVerdict.QUALIFIED,
        resume_profile_id="akash-biswal-fde",
        skills_matched=("platform engineering", "LLM integrations"),
        sponsorship_signal=SponsorshipStatus.YES,
    )
    qualified = build_outreach(scale_posting(), base, fde_profile())
    manual = build_outreach(
        scale_posting(),
        base.model_copy(
            update={
                "verdict": ScoreVerdict.MANUAL_REVIEW,
                "sponsorship_signal": SponsorshipStatus.UNKNOWN,
            }
        ),
        fde_profile(),
    )

    assert not any("sponsorship" in question.casefold() for question in qualified.questions)
    assert sum("sponsorship" in question.casefold() for question in manual.questions) == 1
