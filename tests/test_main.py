"""API tests for JobRadar health and scoring endpoints."""

from pathlib import Path

from fastapi.testclient import TestClient

from agent.cover_letter import CoverLetterGenerationError
from agent.contact_discovery import ContactSuggestion, ContactType
from agent.posting_extractor import ClosedJobPostingError, PostingExtractionError
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


class StubContactFinder:
    def __init__(self) -> None:
        self.calls = 0

    async def find(self, posting, *, job_url: str, limit: int):
        self.calls += 1
        return (
            ContactSuggestion(
                name="Jane Recruiter",
                title=f"Technical Recruiter at {posting.company}",
                contact_type=ContactType.RECRUITER,
                profile_url="https://www.linkedin.com/in/jane-recruiter",
                source="LinkedIn public search result",
                confidence=94,
                evidence="Public result; verify current employment before outreach.",
            ),
        )


class StubCoverLetterGenerator:
    def __init__(self, result: str | Exception | None = None) -> None:
        self.calls = []
        self.result = result or (
            "Acme builds useful backend systems. This role matches verified work."
        )

    async def generate(self, posting, score, resume) -> str:
        self.calls.append((posting, score, resume))
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
            "location": "Los Angeles, CA",
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
    contact_finder: StubContactFinder | None = None,
    cover_letter_generator: StubCoverLetterGenerator | None = None,
    runtime_log_directory: Path | None = None,
) -> TestClient:
    result = provider_result if provider_result is not None else assessment()
    app = create_app(
        database_path=tmp_path / "api.db",
        scoring_engine=ScoringEngine(StubProvider(result)),
        resume_catalog=ResumeCatalog(profiles or (ready_profile(),)),
        posting_extractor=posting_extractor,
        contact_finder=contact_finder,
        cover_letter_generator=cover_letter_generator,
        runtime_log_directory=runtime_log_directory,
    )
    return TestClient(app)


def test_health_initializes_and_checks_database(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ready"
    assert body["schema_version"] == 7
    assert isinstance(body["gemini"]["configured"], bool)
    assert (tmp_path / "api.db").is_file()


def test_discovery_health_reports_latest_runner_failure(tmp_path: Path) -> None:
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    (log_directory / "last_run_status.txt").write_text(
        "[2026-08-23T15:59:09-07:00] Exit code: 1\n",
        encoding="utf-8",
    )
    with make_client(tmp_path, runtime_log_directory=log_directory) as client:
        response = client.get("/api/discovery/status")

    assert response.status_code == 200
    assert response.json() == {
        "status": "failed",
        "last_run_at": "2026-08-23T15:59:09-07:00",
        "exit_code": 1,
        "message": "Scheduled discovery failed. Check logs/errors_in_last_run.txt.",
    }


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


def test_score_rejects_posting_without_verified_us_location(tmp_path: Path) -> None:
    payload = score_payload()
    payload["posting"].pop("location")
    with make_client(tmp_path) as client:
        response = client.post("/api/score", json=payload)

    assert response.status_code == 422
    assert "Only verified US roles are accepted" in response.json()["detail"]


def test_score_url_extracts_scores_and_saves_public_posting(tmp_path: Path) -> None:
    extractor = StubPostingExtractor(
        JobPostingFacts(
            title="Backend Engineer",
            company="Acme",
            description="Build Python APIs.",
            location="Los Angeles, CA",
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


def test_score_url_rejects_closed_posting(tmp_path: Path) -> None:
    extractor = StubPostingExtractor(
        ClosedJobPostingError("https://jobs.example.com/roles/42 is closed")
    )
    with make_client(tmp_path, posting_extractor=extractor) as client:
        response = client.post(
            "/api/score-url",
            json={"url": "https://jobs.example.com/roles/42"},
        )

    assert response.status_code == 422
    assert "closed or expired" in response.json()["detail"]


def test_availability_check_hides_a_stored_closed_job(tmp_path: Path) -> None:
    extractor = StubPostingExtractor(
        ClosedJobPostingError("Page reports closure: This job has closed.")
    )
    with make_client(tmp_path, posting_extractor=extractor) as client:
        job_id = client.post(
            "/api/score", json=score_payload(with_url=True)
        ).json()["job_id"]
        response = client.post(f"/api/jobs/{job_id}/check-availability")
        jobs = client.get("/api/jobs").json()

    assert response.status_code == 200
    assert response.json()["closed"] is True
    assert jobs == []


def test_score_url_rejects_local_network_targets(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/score-url",
            json={"url": "http://127.0.0.1/private-job"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "Enter a public job-posting URL."


def test_score_url_rejects_non_us_posting(tmp_path: Path) -> None:
    extractor = StubPostingExtractor(
        JobPostingFacts(
            title="Backend Engineer",
            company="Acme India",
            description="Build Python APIs.",
            location="Bengaluru, India",
        )
    )
    with make_client(tmp_path, posting_extractor=extractor) as client:
        response = client.post(
            "/api/score-url",
            json={"url": "https://jobs.example.com/roles/india"},
        )

    assert response.status_code == 422
    assert "United States" in response.json()["detail"]


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


def test_scored_job_outreach_matches_digest_guidance(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        job_id = client.post(
            "/api/score", json=score_payload(with_url=True)
        ).json()["job_id"]
        response = client.get(f"/api/jobs/{job_id}/outreach")

    assert response.status_code == 200
    body = response.json()
    assert body["company"] == "Acme"
    assert body["location"] == "Los Angeles, CA"
    assert body["strategy"].startswith("Apply now")
    assert "backend engineering and Python" in body["recruiter_message"]
    assert "right recruiting team" in body["recruiter_message"]


def test_cover_letter_endpoint_returns_plain_text_for_stored_score(tmp_path: Path) -> None:
    generator = StubCoverLetterGenerator()
    with make_client(tmp_path, cover_letter_generator=generator) as client:
        job_id = client.post(
            "/api/score", json=score_payload(with_url=True)
        ).json()["job_id"]
        response = client.post("/api/cover-letter", json={"job_id": job_id})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text.startswith("Acme builds")
    assert generator.calls[0][1].skills_matched == ("backend engineering", "Python")


def test_cover_letter_provider_failure_returns_useful_503(tmp_path: Path) -> None:
    generator = StubCoverLetterGenerator(
        CoverLetterGenerationError("Gemini model is unavailable.")
    )
    with make_client(tmp_path, cover_letter_generator=generator) as client:
        job_id = client.post(
            "/api/score", json=score_payload(with_url=True)
        ).json()["job_id"]
        response = client.post("/api/cover-letter", json={"job_id": job_id})

    assert response.status_code == 503
    assert response.json()["detail"] == "Gemini model is unavailable."


def test_any_user_selected_job_can_be_deleted(tmp_path: Path) -> None:
    rejected_payload = score_payload(with_url=True)
    rejected_payload["posting"]["sponsorship_status"] = "NO"
    rejected_payload["posting"]["description"] = "No visa sponsorship is available."
    rejected_payload["url"] = "https://example.com/jobs/rejected"
    qualified_payload = score_payload(with_url=True)
    qualified_payload["posting"]["title"] = "Qualified Backend Engineer"
    with make_client(tmp_path) as client:
        rejected_id = client.post("/api/score", json=rejected_payload).json()["job_id"]
        qualified_id = client.post("/api/score", json=qualified_payload).json()["job_id"]

        rejected_response = client.delete(f"/api/jobs/{rejected_id}")
        qualified_response = client.delete(f"/api/jobs/{qualified_id}")

        assert rejected_response.status_code == 204
        assert qualified_response.status_code == 204
        assert client.get("/api/jobs").json() == []


def test_contact_search_is_cached_and_can_be_refreshed(tmp_path: Path) -> None:
    finder = StubContactFinder()
    with make_client(tmp_path, contact_finder=finder) as client:
        job_id = client.post(
            "/api/score", json=score_payload(with_url=True)
        ).json()["job_id"]

        first = client.post(f"/api/jobs/{job_id}/contacts", json={})
        cached = client.post(f"/api/jobs/{job_id}/contacts", json={})
        refreshed = client.post(
            f"/api/jobs/{job_id}/contacts", json={"refresh": True}
        )

    assert first.status_code == 200
    assert first.json()["cached"] is False
    assert first.json()["suggestions"][0]["contact_type"] == "RECRUITER"
    assert cached.json()["cached"] is True
    assert refreshed.json()["cached"] is False
    assert finder.calls == 2


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
    assert "APPLICATION PLAYBOOK" in response.text
    assert "Outreach details" in response.text
    assert "People to contact" in response.text
    assert "Checking discovery" in response.text
    assert 'data-status="QUALIFIED"' in response.text
    assert script.status_code == 200
    assert "loadJobs" in script.text
    assert 'fetch("/api/score-url"' in script.text
    assert "recommended_resume" in script.text
    assert "/outreach`" in script.text
    assert "job.location_tier" in script.text
    assert ">Delete</button>" in response.text
    assert "deleteJob" in script.text
    assert "findContacts" in script.text
    assert "Cover Letter" in response.text
    assert 'fetch("/api/cover-letter"' in script.text
    assert 'fetch("/api/discovery/status")' in script.text
    assert 'classList.toggle("current", isCurrentStatus)' in script.text
    assert 'details(job).verdict === "QUALIFIED"' in script.text
    assert 'if (job.status === "APPLIED")' in script.text
    assert 'job.status !== "APPLIED"' in script.text
