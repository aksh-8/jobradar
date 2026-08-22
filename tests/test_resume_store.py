"""Tests for the validated JobRadar resume catalog."""

from datetime import date

import pytest
from pydantic import ValidationError

from backend.resume_store import (
    AKASH_BISWAL_DRAFT,
    AUTO_PROFILE_ID,
    RESUME_CATALOG,
    DuplicateResumeProfileError,
    ResumeCatalog,
    ResumeProfile,
    ResumeProfileNotFoundError,
    ResumeStatus,
    Project,
    Skill,
    WorkExperience,
    configured_resume_catalog,
    load_resume_catalog,
    select_resume_profile,
)


def make_ready_profile(
    profile_id: str,
    target_role: str,
    skill_name: str,
    *,
    aliases: tuple[str, ...] = (),
    keywords: tuple[str, ...] = (),
) -> ResumeProfile:
    return ResumeProfile(
        profile_id=profile_id,
        full_name="Test Candidate",
        status=ResumeStatus.READY,
        summary="Verified test profile.",
        target_roles=(target_role,),
        skills=(
            Skill(
                name=skill_name,
                category="Engineering",
                aliases=aliases,
            ),
        ),
        keywords=keywords,
    )


def test_default_profile_contains_only_verified_identity_and_is_draft() -> None:
    profile = RESUME_CATALOG.get("akash-biswal")

    assert profile is AKASH_BISWAL_DRAFT
    assert profile.full_name == "Akash Biswal"
    assert profile.status is ResumeStatus.DRAFT
    assert profile.skills == ()
    assert RESUME_CATALOG.list_profiles(ready_only=True) == ()


def test_ready_profile_requires_scoring_content() -> None:
    with pytest.raises(ValidationError, match="summary, target_roles, skills"):
        ResumeProfile(
            profile_id="incomplete",
            full_name="Test Candidate",
            status=ResumeStatus.READY,
        )


def test_profile_rejects_unknown_experience_skills() -> None:
    with pytest.raises(ValidationError, match="unknown skills: Rust"):
        ResumeProfile(
            profile_id="backend",
            full_name="Test Candidate",
            skills=(Skill(name="Python", category="Languages"),),
            experience=(
                WorkExperience(
                    company="Acme",
                    title="Engineer",
                    start_date=date(2024, 1, 1),
                    skill_names=("Python", "Rust"),
                ),
            ),
        )


def test_catalog_rejects_duplicate_profile_ids() -> None:
    profile = make_ready_profile("backend", "Backend Engineer", "Python")

    with pytest.raises(DuplicateResumeProfileError, match="backend"):
        ResumeCatalog((profile, profile))


def test_catalog_reports_available_profiles_for_unknown_id() -> None:
    catalog = ResumeCatalog(
        (make_ready_profile("backend", "Backend Engineer", "Python"),)
    )

    with pytest.raises(
        ResumeProfileNotFoundError,
        match="available profiles: backend",
    ):
        catalog.get("missing")


def test_catalog_ranks_ready_profiles_by_explicit_job_terms() -> None:
    backend = make_ready_profile(
        "backend",
        "Backend Engineer",
        "Python",
        aliases=("Python 3",),
        keywords=("FastAPI",),
    )
    frontend = make_ready_profile(
        "frontend",
        "Frontend Engineer",
        "TypeScript",
        keywords=("React",),
    )
    draft = ResumeProfile(profile_id="draft", full_name="Test Candidate")
    catalog = ResumeCatalog((frontend, draft, backend))

    matches = catalog.rank_for_job(
        "Backend Engineer building FastAPI services with Python 3"
    )

    assert [match.profile.profile_id for match in matches] == ["backend", "frontend"]
    assert matches[0].score == 9
    assert matches[0].matched_terms == (
        "backend engineer",
        "fastapi",
        "python",
        "python 3",
    )
    assert matches[1].score == 0


def test_scoring_context_is_json_ready_and_omits_workflow_status() -> None:
    profile = make_ready_profile("backend", "Backend Engineer", "Python")

    context = profile.scoring_context()

    assert context["profile_id"] == "backend"
    assert context["skills"] == [
        {
            "name": "Python",
            "category": "Engineering",
            "aliases": [],
        }
    ]
    assert "status" not in context


def test_load_private_resume_catalog_from_json(tmp_path) -> None:
    path = tmp_path / "resume.json"
    path.write_text(
        """
        {
          "profile_id": "backend",
          "full_name": "Verified Candidate",
          "status": "ready",
          "summary": "Builds Python services.",
          "target_roles": ["Backend Engineer"],
          "skills": [{"name": "Python", "category": "Languages"}]
        }
        """,
        encoding="utf-8",
    )

    profile = load_resume_catalog(path).get("backend")

    assert profile.status is ResumeStatus.READY
    assert profile.skills[0].name == "Python"


def test_configured_catalog_uses_private_path(tmp_path, monkeypatch) -> None:
    path = tmp_path / "resume.json"
    path.write_text(
        '{"profile_id":"private","full_name":"Verified Candidate"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("RESUME_PROFILE_PATH", str(path))

    assert configured_resume_catalog().get("private").full_name == "Verified Candidate"


def test_catalog_file_can_share_verified_facts_across_variants(tmp_path) -> None:
    path = tmp_path / "resume.json"
    path.write_text(
        """
        {
          "shared": {
            "full_name": "Verified Candidate",
            "status": "ready",
            "summary": "Builds Python services.",
            "skills": [{"name": "Python", "category": "Languages"}]
          },
          "profiles": [
            {
              "profile_id": "general",
              "resume_name": "General",
              "target_roles": ["Software Engineer"]
            },
            {
              "profile_id": "platform",
              "resume_name": "Platform",
              "target_roles": ["Platform Engineer"]
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    catalog = load_resume_catalog(path)

    assert [profile.resume_name for profile in catalog.list_profiles()] == [
        "General",
        "Platform",
    ]
    assert catalog.get("platform").skills[0].name == "Python"


def test_profile_rejects_unknown_project_skills() -> None:
    with pytest.raises(ValidationError, match="Project references unknown skills"):
        ResumeProfile(
            profile_id="project",
            full_name="Verified Candidate",
            projects=(
                Project(
                    name="Project",
                    summary="Built a service.",
                    skill_names=("Unlisted",),
                ),
            ),
        )


def test_auto_selection_prefers_matching_resume_variant() -> None:
    catalog = ResumeCatalog(
        (
            make_ready_profile("general", "Software Engineer", "Python"),
            make_ready_profile("platform", "Platform Engineer", "Kubernetes"),
        )
    )

    selected = select_resume_profile(
        catalog,
        "Platform Engineer building Kubernetes developer tooling",
        AUTO_PROFILE_ID,
    )

    assert selected.profile_id == "platform"
