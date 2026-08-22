"""Integration coverage from search response through delivered digest."""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent.digest import build_digest
from agent.discovery import DiscoveryAgent
from agent.email_delivery import SendGridSender
from agent.posting_extractor import PostingExtractor
from agent.search_providers import BraveSearchProvider
from backend.main import create_app
from backend.resume_store import ResumeCatalog, ResumeProfile, ResumeStatus, Skill
from backend.scoring_engine import (
    GeminiScoringProvider,
    OllamaScoringProvider,
    ProviderAssessment,
    ScoreDimensions,
    ScoringEngine,
)

pytestmark = pytest.mark.integration


def integration_profile() -> ResumeProfile:
    return ResumeProfile(
        profile_id="integration-profile",
        full_name="Integration Candidate",
        status=ResumeStatus.READY,
        summary="Builds Python cloud services.",
        target_roles=("Platform Engineer",),
        skills=(Skill(name="Python", category="Languages"),),
    )


@pytest.mark.asyncio
async def test_search_extract_score_digest_deliver_and_list(tmp_path: Path) -> None:
    job_url = "https://jobs.example.com/platform/7"

    async def search_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Subscription-Token"] == "test-search-key"
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Platform Engineer",
                            "url": job_url,
                            "description": "Python platform opportunity",
                        }
                    ]
                }
            },
        )

    async def page_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == job_url
        return httpx.Response(
            200,
            text="""
                <script type="application/ld+json">
                {
                  "@type": "JobPosting",
                  "title": "Platform Engineer",
                  "hiringOrganization": {"name": "Canopy Systems"},
                  "description": "Build Python cloud services. Visa sponsorship is available.",
                  "baseSalary": {
                    "currency": "USD",
                    "value": {"minValue": 155000, "maxValue": 195000, "unitText": "YEAR"}
                  }
                }
                </script>
            """,
        )

    async def gemini_generate(prompt: str) -> str:
        assert "Canopy Systems" in prompt
        return ProviderAssessment(
            dimensions=ScoreDimensions(
                skills_match=86,
                experience_level=82,
                domain_relevance=90,
                role_type=88,
                compensation_signal=85,
            ),
            matched_requirements=("Python", "cloud services"),
            rationale=("Verified Python experience aligns with the platform role.",),
        ).model_dump_json()

    database_path = tmp_path / "discovery-workflow.db"
    scoring_engine = ScoringEngine(
        GeminiScoringProvider(generate=gemini_generate),
        OllamaScoringProvider(
            base_url="http://ollama.test",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(500, text="not expected")
            ),
        ),
    )
    discovery = DiscoveryAgent(
        search_provider=BraveSearchProvider(
            "test-search-key", transport=httpx.MockTransport(search_handler)
        ),
        extractor=PostingExtractor(transport=httpx.MockTransport(page_handler)),
        scoring_engine=scoring_engine,
        resume=integration_profile(),
        database_path=database_path,
    )

    report = await discovery.run(("platform engineer sponsorship",), limit=5)
    digest = build_digest(report.opportunities, run_date=date(2026, 8, 21))

    delivered: dict[str, object] = {}

    async def send_handler(request: httpx.Request) -> httpx.Response:
        delivered.update(json.loads(request.content))
        return httpx.Response(202)

    await SendGridSender(
        api_key="test-send-key",
        from_email="radar@example.com",
        transport=httpx.MockTransport(send_handler),
    ).send(digest, "candidate@example.com")

    assert report.searched_results == 1
    assert len(report.opportunities) == 1
    assert report.opportunities[0].score.overall_score == 86
    assert "Canopy Systems - Platform Engineer" in digest.text
    assert "Resume to use:" in digest.text
    assert "APPLY+REFERRAL" in digest.text
    assert delivered["subject"] == digest.subject

    app = create_app(
        database_path=database_path,
        scoring_engine=scoring_engine,
        resume_catalog=ResumeCatalog((integration_profile(),)),
    )
    with TestClient(app) as client:
        jobs = client.get("/api/jobs").json()

    assert len(jobs) == 1
    assert jobs[0]["company"] == "Canopy Systems"
    assert jobs[0]["source"] == "brave"


@pytest.mark.asyncio
async def test_invalid_primary_output_falls_back_to_ollama_through_api(
    tmp_path: Path,
) -> None:
    async def invalid_gemini(_prompt: str) -> str:
        return "not json"

    assessment = ProviderAssessment(
        dimensions=ScoreDimensions(
            skills_match=75,
            experience_level=75,
            domain_relevance=75,
            role_type=75,
            compensation_signal=75,
        ),
        rationale=("Local fallback produced a valid assessment.",),
    )

    async def ollama_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(
            200,
            json={"message": {"content": assessment.model_dump_json()}},
        )

    engine = ScoringEngine(
        GeminiScoringProvider(generate=invalid_gemini),
        OllamaScoringProvider(
            base_url="http://ollama.test",
            transport=httpx.MockTransport(ollama_handler),
        ),
    )
    app = create_app(
        database_path=tmp_path / "fallback.db",
        scoring_engine=engine,
        resume_catalog=ResumeCatalog((integration_profile(),)),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/score",
            json={
                "profile_id": "integration-profile",
                "posting": {
                    "title": "Platform Engineer",
                    "company": "Canopy Systems",
                    "description": "Build Python commercial cloud services.",
                    "sponsorship_status": "YES",
                    "company_size": 500,
                    "base_salary_max_usd": 180000,
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["provider"] == "ollama"
    assert response.json()["fallback_used"] is True
