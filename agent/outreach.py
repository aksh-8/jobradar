"""Deterministic outreach suggestions derived from verified scoring evidence."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from backend.red_flag_scanner import JobPostingFacts
from backend.scoring_engine import ScoreVerdict, ScoringResult


class OutreachGuidance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str
    opening: str
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
    return OutreachGuidance(
        subject=f"Interest in {posting.title} at {posting.company}",
        opening=(
            f"I am interested in the {posting.title} opportunity at "
            f"{posting.company} and would value a brief conversation."
        ),
        talking_points=talking_points,
        questions=questions,
    )
