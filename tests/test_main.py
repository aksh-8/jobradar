"""API tests for JobRadar health and scoring endpoints."""

from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import create_app
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
            role_alignment=score,
            required_skills=score,
            experience_fit=score,
            career_fit=score,
        ),
        rationale=("The verified profile matches the posting.",),
    )


def score_payload(profile_id: str = "backend") -> dict[str, object]:
    return {
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


def make_client(
    tmp_path: Path,
    *,
    provider_result: ProviderAssessment | Exception | None = None,
    profiles: tuple[ResumeProfile, ...] | None = None,
) -> TestClient:
    result = provider_result if provider_result is not None else assessment()
    app = create_app(
        database_path=tmp_path / "api.db",
        scoring_engine=ScoringEngine(StubProvider(result)),
        resume_catalog=ResumeCatalog(profiles or (ready_profile(),)),
    )
    return TestClient(app)


def test_health_initializes_and_checks_database(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ready"
    assert body["schema_version"] == 1
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
