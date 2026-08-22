"""Tests for outreach, digest rendering, and explicit email adapters."""

import json
from datetime import date

import httpx
import pytest

from agent.digest import DigestOpportunity, build_digest
from agent.email_delivery import EmailDeliveryError, SendGridSender
from agent.outreach import build_outreach
from backend.red_flag_scanner import JobPostingFacts, SponsorshipStatus
from backend.scoring_engine import ScoreDimensions, ScoreVerdict, ScoringResult


def opportunity() -> DigestOpportunity:
    posting = JobPostingFacts(
        title="Backend <Engineer>",
        company="Acme & Co",
        description="Build Python services.",
        sponsorship_status=SponsorshipStatus.UNKNOWN,
    )
    score = ScoringResult(
        overall_score=88,
        verdict=ScoreVerdict.MANUAL_REVIEW,
        provider="test",
        dimensions=ScoreDimensions(
            skills_match=90,
            experience_level=85,
            domain_relevance=90,
            role_type=82,
            compensation_signal=75,
        ),
        matched_requirements=("Python",),
        missing_requirements=("Kubernetes",),
        rationale=("Strong fit.",),
    )
    return DigestOpportunity(
        job_id=1,
        url="https://example.com/jobs/1?a=1&b=2",
        posting=posting,
        score=score,
        outreach=build_outreach(posting, score),
    )


def test_outreach_uses_score_evidence_and_review_question() -> None:
    guidance = opportunity().outreach

    assert guidance.talking_points == ("Python",)
    assert "Kubernetes" in guidance.questions[0]
    assert "sponsorship" in guidance.questions[1]


def test_digest_orders_and_html_escapes_content() -> None:
    digest = build_digest((opportunity(),), run_date=date(2026, 8, 21))

    assert "Acme & Co - Backend <Engineer>" in digest.text
    assert "Score: 88/100" in digest.text
    assert "LINKEDIN RECRUITER MESSAGE" in digest.text
    assert "Backend &lt;Engineer&gt;" in digest.html
    assert "Acme &amp; Co" in digest.html
    assert "a=1&amp;b=2" in digest.html


@pytest.mark.asyncio
async def test_sendgrid_posts_both_digest_formats() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sendgrid-key"
        body = json.loads(request.content)
        assert body["personalizations"][0]["to"][0]["email"] == "to@example.com"
        assert [item["type"] for item in body["content"]] == [
            "text/plain",
            "text/html",
        ]
        return httpx.Response(202)

    sender = SendGridSender(
        api_key="sendgrid-key",
        from_email="from@example.com",
        transport=httpx.MockTransport(handler),
    )
    await sender.send(
        build_digest((opportunity(),), run_date=date(2026, 8, 21)),
        "to@example.com",
    )


@pytest.mark.asyncio
async def test_sendgrid_requires_configuration() -> None:
    with pytest.raises(EmailDeliveryError, match="SENDGRID_API_KEY"):
        await SendGridSender(
            api_key="replace_with_sendgrid_api_key",
            from_email="from@example.com",
        ).send(build_digest(()), "to@example.com")
