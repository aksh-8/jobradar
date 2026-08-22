"""Plain-text and HTML daily digest rendering."""

from __future__ import annotations

from datetime import date
from html import escape

from pydantic import BaseModel, ConfigDict

from agent.outreach import OutreachGuidance
from backend.red_flag_scanner import JobPostingFacts
from backend.scoring_engine import ScoringResult


class DigestOpportunity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: int
    url: str
    posting: JobPostingFacts
    score: ScoringResult
    outreach: OutreachGuidance


class Digest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str
    text: str
    html: str


def build_digest(
    opportunities: tuple[DigestOpportunity, ...], *, run_date: date | None = None
) -> Digest:
    day = run_date or date.today()
    ordered = tuple(
        sorted(opportunities, key=lambda item: (-item.score.overall_score, item.job_id))
    )
    subject = f"JobRadar daily digest - {len(ordered)} opportunities - {day.isoformat()}"
    text_lines = [subject, ""]
    html_items = []
    for item in ordered:
        posting = item.posting
        text_lines.extend(
            (
                f"{item.score.overall_score}/100 - {posting.title} at {posting.company}",
                f"Verdict: {item.score.verdict.value}",
                f"Link: {item.url}",
                f"Outreach: {item.outreach.opening}",
            )
        )
        for point in item.outreach.talking_points:
            text_lines.append(f"- {point}")
        for question in item.outreach.questions:
            text_lines.append(f"Question: {question}")
        text_lines.append("")

        rationale = " ".join(item.score.rationale)
        html_items.append(
            "<article style='padding:16px 0;border-bottom:1px solid #dfe5df'>"
            f"<h2 style='margin:0'>{escape(posting.title)}</h2>"
            f"<p><strong>{escape(posting.company)}</strong> · "
            f"{item.score.overall_score}/100 · {escape(item.score.verdict.value)}</p>"
            f"<p>{escape(rationale)}</p>"
            f"<p><a href='{escape(item.url, quote=True)}'>Open job posting</a></p>"
            f"<p><strong>Outreach:</strong> {escape(item.outreach.opening)}</p>"
            "</article>"
        )
    if not ordered:
        text_lines.append("No new qualified jobs were found today.")
        html_items.append("<p>No new qualified jobs were found today.</p>")
    html = (
        "<!doctype html><html><body style='font-family:Arial,sans-serif;color:#172019'>"
        f"<h1>JobRadar daily digest</h1><p>{day.isoformat()}</p>"
        + "".join(html_items)
        + "</body></html>"
    )
    return Digest(subject=subject, text="\n".join(text_lines).rstrip(), html=html)
