"""Plain-text and HTML compiled digest rendering."""

from __future__ import annotations

from datetime import date
from html import escape
import re

from pydantic import BaseModel, ConfigDict

from agent.outreach import OutreachGuidance
from backend.job_store import FollowUpReminder
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
    opportunities: tuple[DigestOpportunity, ...],
    *,
    run_date: date | None = None,
    follow_ups: tuple[FollowUpReminder, ...] = (),
    weekly_gaps: tuple[tuple[str, int], ...] = (),
) -> Digest:
    day = run_date or date.today()
    ordered = tuple(
        sorted(
            opportunities,
            key=lambda item: (
                -(item.score.overall_score + _location_boost(item.posting)),
                -item.score.overall_score,
                item.job_id,
            ),
        )
    )
    subject = f"JobRadar digest - {len(ordered)} new roles - {day.isoformat()}"
    text_lines = [subject, ""]
    html_items: list[str] = []

    if follow_ups:
        text_lines.extend(("FOLLOW-UP REMINDERS", ""))
        for reminder in follow_ups:
            text_lines.extend(
                (
                    f"{reminder.company} - {reminder.title}",
                    f"Applied: {reminder.applied_at}",
                    f"URL: {reminder.url}",
                    "Action: Send a concise follow-up now.",
                    "",
                )
            )
        follow_up_items = "".join(
            "<li>"
            f"<a href='{escape(item.url, quote=True)}'>{escape(item.company)} - "
            f"{escape(item.title)}</a> (applied {escape(item.applied_at)})"
            "</li>"
            for item in follow_ups
        )
        html_items.append(
            "<section><h2>Follow-up reminders</h2><ul>"
            + follow_up_items
            + "</ul></section>"
        )

    for item in ordered:
        posting = item.posting
        sponsorship = (
            item.score.sponsorship_signal.value
            if item.score.sponsorship_signal is not None
            else posting.sponsorship_status.value
        )
        action = (
            item.score.action_verdict.value
            if item.score.action_verdict is not None
            else item.score.verdict.value
        )
        salary = _salary_text(posting)
        missing = ", ".join(item.score.missing_requirements) or "None identified"
        text_lines.extend(
            (
                f"{posting.company} - {posting.title}",
                f"Score: {item.score.overall_score}/100 | {action} | {sponsorship}",
                f"Salary: {salary}",
                f"Location: {posting.location or 'Unknown'}"
                f" ({posting.workplace_type or 'workplace type unknown'})",
                f"URL: {item.url}",
                f"Resume to use: {item.score.recommended_resume or 'Manual selection required'}",
                "",
                "OUTREACH STRATEGY:",
                item.outreach.strategy,
                "",
                "LINKEDIN RECRUITER MESSAGE:",
                item.outreach.recruiter_message,
                "",
                "REFERRAL REQUEST MESSAGE:",
                item.outreach.referral_message,
                "",
                "COLD EMAIL:",
                item.outreach.cold_email,
                "",
                f"MISSING SKILLS TO ADDRESS: {missing}",
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
            f"<h2 style='margin:0'>{escape(posting.company)} - {escape(posting.title)}</h2>"
            f"<p>{item.score.overall_score}/100 | {escape(action)} | "
            f"{escape(sponsorship)}</p>"
            f"<p><strong>Salary:</strong> {escape(salary)}<br>"
            f"<strong>Location:</strong> {escape(posting.location or 'Unknown')} "
            f"({escape(posting.workplace_type or 'workplace type unknown')})<br>"
            f"<strong>Resume:</strong> "
            f"{escape(item.score.recommended_resume or 'Manual selection required')}</p>"
            f"<p>{escape(rationale)}</p>"
            f"<p><a href='{escape(item.url, quote=True)}'>Open job posting</a></p>"
            f"<p><strong>Strategy:</strong> {escape(item.outreach.strategy)}</p>"
            f"<p><strong>Recruiter message:</strong> "
            f"{escape(item.outreach.recruiter_message)}</p>"
            f"<p><strong>Referral request:</strong> "
            f"{escape(item.outreach.referral_message)}</p>"
            f"<p><strong>Cold email:</strong> {escape(item.outreach.cold_email)}</p>"
            f"<p><strong>Missing skills:</strong> {escape(missing)}</p>"
            "</article>"
        )

    if not ordered:
        text_lines.append("No new qualified jobs were found in this run.")
        html_items.append("<p>No new qualified jobs were found in this run.</p>")

    if weekly_gaps:
        text_lines.extend(("", "WEEKLY SKILLS GAP SUMMARY"))
        for skill, count in weekly_gaps:
            text_lines.append(f"- {skill}: missing from {count} role(s)")
        gap_items = "".join(
            f"<li>{escape(skill)}: missing from {count} role(s)</li>"
            for skill, count in weekly_gaps
        )
        html_items.append(
            "<section><h2>Weekly skills gap summary</h2><ul>"
            + gap_items
            + "</ul></section>"
        )

    html = (
        "<!doctype html><html><body style='font-family:Arial,sans-serif;color:#172019'>"
        f"<h1>JobRadar digest</h1><p>{day.isoformat()}</p>"
        + "".join(html_items)
        + "</body></html>"
    )
    return Digest(subject=subject, text="\n".join(text_lines).rstrip(), html=html)


def _salary_text(posting: JobPostingFacts) -> str:
    minimum = posting.base_salary_min_usd
    maximum = posting.base_salary_max_usd
    if minimum is not None and maximum is not None:
        return f"${minimum:,}-${maximum:,} base"
    if minimum is not None:
        return f"From ${minimum:,} base"
    if maximum is not None:
        return f"Up to ${maximum:,} base"
    return "Unknown - verify before applying"


def _location_boost(posting: JobPostingFacts) -> int:
    """Prefer LA and US-remote roles without rejecting other US locations."""

    location = (posting.location or "").casefold()
    workplace = (posting.workplace_type or "").casefold()
    if "los angeles" in location:
        return 5
    if "remote" in location or "remote" in workplace:
        return 3
    if "california" in location or re.search(r"\bca\b", location):
        return 2
    return 0
