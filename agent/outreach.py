"""Deterministic outreach suggestions derived from verified scoring evidence."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from backend.candidate_evidence import fallback_candidate_skills
from backend.red_flag_scanner import JobPostingFacts
from backend.resume_store import ResumeProfile
from backend.scoring_engine import ScoreVerdict, ScoringResult


class OutreachGuidance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str
    strategy: str
    opening: str
    recruiter_message: str
    referral_message: str
    cold_email: str
    talking_points: tuple[str, ...]
    questions: tuple[str, ...]


def build_outreach(
    posting: JobPostingFacts,
    result: ScoringResult,
    resume: ResumeProfile | None = None,
) -> OutreachGuidance:
    matched = tuple(result.skills_matched[:3]) or fallback_candidate_skills(resume)
    talking_points = matched
    questions = tuple(
        f"Could you share how important {requirement} is in the first six months?"
        for requirement in result.missing_requirements[:2]
    )
    if result.verdict is ScoreVerdict.MANUAL_REVIEW:
        questions += ("Is employment visa sponsorship available for this position?",)
    evidence = _joined_skills(matched)
    pitch = f"My background in {evidence} maps directly to this role."
    strategy = "Apply now on the careers page and pursue a referral simultaneously."
    return OutreachGuidance(
        subject=f"Interest in {posting.title} at {posting.company}",
        strategy=strategy,
        opening=(
            f"I applied for the {posting.title} role at {posting.company}."
        ),
        recruiter_message=(
            f"Hi - I applied for the {posting.title} role at {posting.company}. "
            f"{pitch} "
            "Could you confirm that my application reached the right recruiting team?"
        ),
        referral_message=(
            f"Hi - I applied for the {posting.title} role at {posting.company}. "
            f"{pitch} "
            "After reviewing the posting, would you be comfortable referring me "
            "if the fit looks credible?"
        ),
        cold_email=(
            f"I came across the {posting.title} role at {posting.company} and applied. "
            f"{pitch} "
            "The work looks closely aligned with how I build and ship systems. "
            "Would you be open to a short conversation about the team's priorities?"
        ),
        talking_points=talking_points,
        questions=questions,
    )


def _joined_skills(skills: tuple[str, ...]) -> str:
    selected = skills[:3]
    if len(selected) == 1:
        return selected[0]
    if len(selected) == 2:
        return f"{selected[0]} and {selected[1]}"
    return f"{selected[0]}, {selected[1]}, and {selected[2]}"
