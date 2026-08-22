"""Deterministic outreach suggestions derived from verified scoring evidence."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from backend.red_flag_scanner import JobPostingFacts
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
    posting: JobPostingFacts, result: ScoringResult
) -> OutreachGuidance:
    matched = tuple(result.matched_requirements[:3])
    talking_points = matched or tuple(result.rationale[:2])
    questions = tuple(
        f"Could you share how important {requirement} is in the first six months?"
        for requirement in result.missing_requirements[:2]
    )
    if result.verdict is ScoreVerdict.MANUAL_REVIEW:
        questions += ("Is employment visa sponsorship available for this position?",)
    evidence = matched[0] if matched else "platform and automation engineering"
    strategy = "Apply now on the careers page and pursue a referral simultaneously."
    return OutreachGuidance(
        subject=f"Interest in {posting.title} at {posting.company}",
        strategy=strategy,
        opening=(
            f"I am interested in the {posting.title} opportunity at "
            f"{posting.company} and would value a brief conversation."
        ),
        recruiter_message=(
            f"Hi - I applied for the {posting.title} role at {posting.company}. "
            f"My recent work includes {evidence}, production automation, and "
            "end-to-end platform delivery. The role looks closely aligned. "
            "Could you point me to the recruiter or hiring manager responsible?"
        ),
        referral_message=(
            f"Hi - I just applied for the {posting.title} role at {posting.company}. "
            f"My background in {evidence} and platform automation appears relevant. "
            "If you think the fit is credible after reviewing the posting, would "
            "you be comfortable referring me or sharing the hiring team's context?"
        ),
        cold_email=(
            f"I applied for the {posting.title} role at {posting.company}. My work "
            f"in {evidence}, backend systems, and automation maps directly to the "
            "role. I would welcome a short conversation about the team's current "
            "priorities and where this background could contribute immediately."
        ),
        talking_points=talking_points,
        questions=questions,
    )
