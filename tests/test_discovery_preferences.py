from datetime import datetime, timedelta, timezone

import pytest

from backend.red_flag_scanner import is_internship
from backend.scoring_engine import GeminiScoringProvider, ScoringEngine
from backend.job_store import save_scored_job, list_jobs, list_pending_digest_jobs
from backend.monthly_outlook import monthly_outlook
from backend.database import initialize_database
from backend.discovery_priority import posting_age_hours, priority_key
from agent.search_providers import SearchResult
from tests.test_scoring_engine import make_assessment, make_posting, make_resume, StubProvider
from tests.test_job_store import posting, result


@pytest.mark.parametrize('title', ['Software Engineer Intern', 'AI Internship', 'Intership Engineer'])
@pytest.mark.asyncio
async def test_internships_never_call_model(title):
    provider = StubProvider('test', make_assessment())
    outcome = await ScoringEngine(provider).score(make_posting(title=title), make_resume())
    assert outcome.hard_flags[0].code.value == 'INTERNSHIP'
    assert provider.calls == 0


def test_internship_boundaries():
    assert not is_internship('Internal Tools Engineer')
    assert is_internship('Software Engineer', 'INTERNSHIP')


@pytest.mark.asyncio
async def test_old_internship_records_hidden_and_excluded_from_outlook(tmp_path):
    path = tmp_path / 'jobs.db'
    await initialize_database(path)
    await save_scored_job(posting().model_copy(update={'title': 'Platform Intern'}), result(),
                          source='brave', url='https://example.com/intern', database_path=path)
    assert await list_jobs(database_path=path) == ()
    assert await list_pending_digest_jobs(database_path=path) == ()
    assert (await monthly_outlook(path))['roles_reviewed'] == 0


@pytest.mark.asyncio
async def test_identical_scoring_is_reused_but_changed_evidence_is_not():
    calls = []
    async def generate(prompt):
        calls.append(prompt)
        return make_assessment().model_dump_json()
    provider = GeminiScoringProvider(generate=generate)
    await provider.score(make_posting(), make_resume())
    await provider.score(make_posting(), make_resume())
    assert len(calls) == 1
    await provider.score(make_posting(description='Different requirements'), make_resume())
    assert len(calls) == 2


def test_freshness_and_local_priority():
    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert 1 < posting_age_hours(recent) < 3
    assert posting_age_hours('unknown') is None
    local = SearchResult(title='Engineer', url='https://example.com/1', location='Culver City, CA')
    distant = local.model_copy(update={'location': 'New York, NY', 'date_posted': recent})
    assert priority_key(local) < priority_key(distant)


@pytest.mark.asyncio
async def test_monthly_outlook_has_evidence_and_action(tmp_path):
    path = tmp_path / 'jobs.db'
    await initialize_database(path)
    score = result().model_copy(update={'missing_requirements': ('Kubernetes',)})
    await save_scored_job(posting(), score, source='brave', url='https://example.com/1', database_path=path)
    report = await monthly_outlook(path)
    assert report['roles_scored'] == 1
    assert report['actions'][0]['gap'] == 'kubernetes'
    assert report['actions'][0]['examples'] == ['Acme — Backend Engineer']
    assert 'Week 4' in report['actions'][0]['action']
