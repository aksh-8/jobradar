from datetime import date
from backend.role_targeting import required_years, level_penalty, verified_years
from backend.resume_store import WorkExperience
from tests.test_scoring_engine import make_resume
import pytest
from backend.database import initialize_database
from backend.job_store import save_scored_job, update_job_status, JobStatus
from backend.resume_store import ResumeCatalog
from backend.shortlist import build_shortlist
from tests.test_job_store import posting, result


def test_numeric_years_and_staff_priority():
    assert required_years('8+ years of software engineering experience') == 8
    assert required_years('5-8 years of experience') == 5
    assert required_years('Founded 8 years ago') is None
    assert level_penalty('Staff Software Engineer') == 1
    assert level_penalty('Senior Software Engineer', '3 years of experience') == 0


def test_overlapping_employment_not_double_counted():
    profile = make_resume().model_copy(update={'experience': (
        WorkExperience(company='A', title='Engineer', start_date=date(2020, 1, 1), end_date=date(2022, 1, 1)),
        WorkExperience(company='B', title='Engineer', start_date=date(2021, 1, 1), end_date=date(2023, 1, 1)),
    )})
    assert verified_years(profile) == 3.0


@pytest.mark.asyncio
async def test_shortlist_matches_resume_and_removes_applied(tmp_path):
    path = tmp_path / 'shortlist.db'
    await initialize_database(path)
    profile = make_resume().model_copy(update={'experience': (
        WorkExperience(company='A', title='Engineer', start_date=date(2020, 1, 1), end_date=date(2024, 1, 1)),
    )})
    score = result().model_copy(update={'resume_profile_id': profile.profile_id})
    job_id = await save_scored_job(posting().model_copy(update={'title': 'Platform Engineer'}),
                                  score, source='brave', url='https://example.com/1', database_path=path)
    catalog = ResumeCatalog((profile,))
    report = await build_shortlist(catalog, path)
    assert len(report['matches']) == 1
    assert report['matches'][0]['id'] == job_id
    await update_job_status(job_id, JobStatus.APPLIED, database_path=path)
    assert (await build_shortlist(catalog, path))['matches'] == []


@pytest.mark.asyncio
async def test_staff_is_reviewed_but_does_not_fill_shortlist(tmp_path):
    path = tmp_path / 'shortlist.db'
    await initialize_database(path)
    profile = make_resume()
    score = result().model_copy(update={'resume_profile_id': profile.profile_id})
    await save_scored_job(posting().model_copy(update={'title': 'Staff Software Engineer'}),
                          score, source='brave', url='https://example.com/2', database_path=path)
    report = await build_shortlist(ResumeCatalog((profile,)), path)
    assert report['matches'] == []
    assert report['reviewed'][0]['concerns']
