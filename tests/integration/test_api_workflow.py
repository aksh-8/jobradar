"""Integration coverage for extension-style scoring and lifecycle persistence."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.database import database_connection
from backend.main import create_app
from backend.resume_store import ResumeCatalog, ResumeProfile, ResumeStatus, Skill
from backend.scoring_engine import ProviderAssessment, ScoreDimensions, ScoringEngine

pytestmark = pytest.mark.integration


class WorkflowProvider:
    name = "integration-provider"

    async def score(self, posting, resume) -> ProviderAssessment:
        return ProviderAssessment(
            dimensions=ScoreDimensions(
                skills_match=88,
                experience_level=84,
                domain_relevance=92,
                role_type=80,
                compensation_signal=90,
            ),
            matched_requirements=("Python", "FastAPI"),
            missing_requirements=("Kubernetes",),
            rationale=("Verified experience aligns with the core requirements.",),
        )


def workflow_profile() -> ResumeProfile:
    return ResumeProfile(
        profile_id="integration-profile",
        full_name="Integration Candidate",
        status=ResumeStatus.READY,
        summary="Builds production Python APIs.",
        target_roles=("Backend Engineer",),
        skills=(
            Skill(name="Python", category="Languages"),
            Skill(name="FastAPI", category="Frameworks"),
        ),
    )


def workflow_app(database_path: Path):
    return create_app(
        database_path=database_path,
        scoring_engine=ScoringEngine(WorkflowProvider()),
        resume_catalog=ResumeCatalog((workflow_profile(),)),
    )


def extension_payload() -> dict[str, object]:
    return {
        "profile_id": "integration-profile",
        "source": "browser-extension",
        "url": "https://jobs.example.com/backend/42?utm_source=extension#apply",
        "posting": {
            "title": "Senior Backend Engineer",
            "company": "Northstar Labs",
            "description": "Build Python and FastAPI services for commercial clients.",
            "sponsorship_status": "YES",
            "company_size": 600,
            "base_salary_min_usd": 165000,
            "base_salary_max_usd": 205000,
        },
    }


def test_extension_score_dashboard_lifecycle_survives_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "workflow.db"
    with TestClient(workflow_app(database_path)) as client:
        health = client.get("/health")
        score = client.post("/api/score", json=extension_payload())

        assert health.status_code == 200
        assert score.status_code == 200
        scored = score.json()
        assert scored["verdict"] == "QUALIFIED"
        assert scored["overall_score"] == 87
        assert scored["job_id"] is not None

        listed = client.get("/api/jobs").json()
        assert len(listed) == 1
        assert listed[0]["url"] == "https://jobs.example.com/backend/42"
        assert listed[0]["score_details"]["matched_requirements"] == [
            "Python",
            "FastAPI",
        ]

        viewed = client.patch(
            f"/api/jobs/{scored['job_id']}/status",
            json={"status": "VIEWED"},
        )
        assert viewed.status_code == 200
        assert viewed.json()["status"] == "VIEWED"

    with TestClient(workflow_app(database_path)) as restarted_client:
        persisted = restarted_client.get("/api/jobs?status=VIEWED")
        dashboard = restarted_client.get("/dashboard/")

    assert persisted.status_code == 200
    assert len(persisted.json()) == 1
    assert dashboard.status_code == 200
    assert "JobRadar Dashboard" in dashboard.text
    assert asyncio.run(event_types(database_path, scored["job_id"])) == [
        "DISCOVERED",
        "VIEWED",
    ]


async def event_types(database_path: Path, job_id: int) -> list[str]:
    async with database_connection(database_path) as connection:
        cursor = await connection.execute(
            "SELECT event_type FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)
        )
        return [row["event_type"] for row in await cursor.fetchall()]


def test_repeated_extension_score_deduplicates_without_resetting_status(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    with TestClient(workflow_app(database_path)) as client:
        first = client.post("/api/score", json=extension_payload()).json()
        client.patch(
            f"/api/jobs/{first['job_id']}/status", json={"status": "APPLIED"}
        )
        repeated = client.post("/api/score", json=extension_payload()).json()
        jobs = client.get("/api/jobs").json()

    assert repeated["job_id"] == first["job_id"]
    assert len(jobs) == 1
    assert jobs[0]["status"] == "APPLIED"
