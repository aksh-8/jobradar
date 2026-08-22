"""API tests for JobRadar health and scoring endpoints."""

from pathlib import Path

from fastapi.testclient import TestClient

from agent.posting_extractor import PostingExtractionError
from backend.main import create_app
from backend.red_flag_scanner import JobPostingFacts, SponsorshipStatus
from backend.resume_store import ResumeCatalog, ResumeProfile, ResumeStatus, Skill
from backend.scoring_engine import (
    ProviderAssessment,
    ScoreDimensions,
    ScoreVerdict,
    ScoringEngine,
    ScoringProviderError,
)


class StubProvider:
    name = "stub"

    def __init__(self, result: ProviderAssessment | Exception) -> None:
        self.result = result

    async def score(self, posting, resume) -> ProviderAssessment:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class StubPostingExtractor:
    def __init__(self, result: JobPostingFacts | Exception) -> None:
        self.result = result
        self.requested_url: str | None = None

    async def fetch(self, result) -> JobPostingFacts:
        self.requested_url = result.url
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def ready_profile() -> ResumeProfile:
    return ResumeProfile(
        profile_id="backend",
        full_name="Test Candidate",
        status=ResumeStatus.READY,
        summary="Builds Python APIs.",
        target_roles=("Backend Engineer",),
        skills=(Skill(name="Python", category="Languages"),),
    )


def assessment(score: int = 80) -> ProviderAssessment:
    return ProviderAssessment(
        dimensions=ScoreDimensions(
            skills_match=score,
            experience_level=score,
            domain_relevance=score,
            role_type=score,
            compensation_signal=score,
        ),
        rationale=("The verified profile matches the posting.",),
    )


def score_payload(
    profile_id: str = "backend", *, with_url: bool = False
) -> dict[str, object]:
    payload: dict[str, object] = {
        "profile_id": profile_id,
        "posting": {
            "title": "Backend Engineer",
            "company": "Acme",
            "description": "Build Python APIs.",
            "sponsorship_status": "YES",
            "company_size": 500,
            "base_salary_max_usd": 180000,
        },
    }
    if with_url:
        payload.update(
            source="test-extension",
            url="https://example.com/jobs/42?utm_source=test",
        )
    return payload


def make_client(
    tmp_path: Path,
    *,
    provider_result: ProviderAssessment | Exception | None = None,
    profiles: tuple[ResumeProfile, ...] | None = None,
    posting_extractor: StubPostingExtractor | None = None,
) -> TestClient:
    result = provider_result if provider_result is not None else assessment()
    app = create_app(
        database_path=tmp_path / "api.db",
        scoring_engine=ScoringEngine(StubProvider(result)),
        resume_catalog=ResumeCatalog(profiles or (ready_profile(),)),
        posting_extractor=posting_extractor,
    )
    return TestClient(app)


def test_health_initializes_and_checks_database(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ready"
    assert body["schema_version"] == 3
    assert isinstance(body["gemini"]["configured"], bool)
    assert (tmp_path / "api.db").is_file()


def test_score_returns_structured_result(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post("/api/score", json=score_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["overall_score"] == 80
    assert body["verdict"] == ScoreVerdict.QUALIFIED
    assert body["provider"] == "stub"


def test_score_rejects_unknown_profile(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post("/api/score", json=score_payload("missing"))

    assert response.status_code == 404
    assert "Unknown resume profile" in response.json()["detail"]


def test_score_rejects_draft_profile(tmp_path: Path) -> None:
    draft = ResumeProfile(profile_id="draft", full_name="Test Candidate")
    with make_client(tmp_path, profiles=(draft,)) as client:
        response = client.post("/api/score", json=score_payload("draft"))

    assert response.status_code == 409
    assert "still a draft" in response.json()["detail"]


def test_score_maps_provider_failure_to_service_unavailable(tmp_path: Path) -> None:
    with make_client(
        tmp_path,
        provider_result=ScoringProviderError("provider unavailable"),
    ) as client:
        response = client.post("/api/score", json=score_payload())

    assert response.status_code == 503
    assert "provider unavailable" in response.json()["detail"]


def test_score_validates_request_before_calling_engine(tmp_path: Path) -> None:
    payload = score_payload()
    payload["posting"]["unexpected"] = True
    with make_client(tmp_path) as client:
        response = client.post("/api/score", json=payload)

    assert response.status_code == 422


def test_score_url_extracts_scores_and_saves_public_posting(tmp_path: Path) -> None:
    extractor = StubPostingExtractor(
        JobPostingFacts(
            title="Backend Engineer",
            company="Acme",
            description="Build Python APIs.",
            sponsorship_status=SponsorshipStatus.YES,
            company_size=500,
            base_salary_max_usd=180000,
        )
    )
    with make_client(tmp_path, posting_extractor=extractor) as client:
        response = client.post(
            "/api/score-url",
            json={"url": "https://jobs.example.com/roles/42", "profile_id": "auto"},
        )
        jobs = client.get("/api/jobs").json()

    assert response.status_code == 200
    assert response.json()["overall_score"] == 80
    assert response.json()["recommended_resume"] == "backend"
    assert response.json()["job_id"] == jobs[0]["id"]
    assert extractor.requested_url == "https://jobs.example.com/roles/42"


def test_score_url_explains_extraction_failure(tmp_path: Path) -> None:
    extractor = StubPostingExtractor(PostingExtractionError("access denied"))
    with make_client(tmp_path, posting_extractor=extractor) as client:
        response = client.post(
            "/api/score-url",
            json={"url": "https://jobs.example.com/roles/42"},
        )

    assert response.status_code == 422
    assert "browser extension" in response.json()["detail"]


def test_score_url_rejects_local_network_targets(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/score-url",
            json={"url": "http://127.0.0.1/private-job"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "Enter a public job-posting URL."


def test_scored_job_is_listed_and_lifecycle_can_change(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        score_response = client.post(
            "/api/score", json=score_payload(with_url=True)
        )
        assert score_response.status_code == 200
        job_id = score_response.json()["job_id"]

        jobs_response = client.get("/api/jobs")
        assert jobs_response.status_code == 200
        assert jobs_response.json()[0]["id"] == job_id

        update_response = client.patch(
            f"/api/jobs/{job_id}/status",
            json={"status": "APPLIED"},
        )
        assert update_response.status_code == 200
        assert update_response.json()["status"] == "APPLIED"


def test_skipping_job_requires_reason(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        job_id = client.post(
            "/api/score", json=score_payload(with_url=True)
        ).json()["job_id"]
        response = client.patch(
            f"/api/jobs/{job_id}/status",
            json={"status": "SKIPPED"},
        )

    assert response.status_code == 422
    assert "skip_reason" in response.json()["detail"]


def test_dashboard_assets_are_served(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/dashboard/")
        script = client.get("/dashboard/app.js")

    assert response.status_code == 200
    assert "APPLICATION COMMAND CENTER" in response.text
    assert "Score a job from its link" in response.text
    assert "USE RESUME" in response.text
    assert script.status_code == 200
    assert "loadJobs" in script.text
    assert 'fetch("/api/score-url"' in script.text
    assert "recommended_resume" in script.text
    assert 'classList.toggle("current", isCurrentStatus)' in script.text
