"""A dynamic shortlist from saved scores; no new model or search requests."""
from backend.job_store import list_jobs, get_scored_job_details
from backend.location_policy import us_location_sort_key
from backend.resume_store import ResumeProfileNotFoundError, ResumeStatus
from backend.role_targeting import level_penalty, required_years, target_role, verified_years


async def build_shortlist(catalog, database_path=None):
    jobs = await list_jobs(us_only=True, database_path=database_path)
    candidates = []
    for job in jobs:
        if job.status.value not in {'DISCOVERED', 'VIEWED'} or not target_role(job.title):
            continue
        details = await get_scored_job_details(job.id, database_path=database_path)
        if details is None:
            continue
        posting, score = details.posting, details.score
        location = us_location_sort_key(posting.location, posting.workplace_type, posting.description)[0]
        if location > 1 or score.hard_flags or score.verdict.value in {'REJECTED', 'BELOW_THRESHOLD'}:
            continue
        if not score.dimensions or not score.resume_profile_id:
            continue
        try:
            profile = catalog.get(score.resume_profile_id)
        except ResumeProfileNotFoundError:
            continue
        if profile.status is not ResumeStatus.READY:
            continue
        years = verified_years(profile)
        required = required_years(posting.description)
        penalty = level_penalty(posting.title, posting.description)
        concerns = []
        if penalty:
            concerns.append('Staff-level title or an 8+ year experience mention; lower priority.')
        if not profile.experience:
            concerns.append('Verified employment history is missing.')
        if required is not None and required > years:
            concerns.append(f'Posting mentions {required}+ years versus {years} years of verified employment; review alternatives and skill-specific requirements.')
        if score.dimensions.experience_level < 60 or score.dimensions.skills_match < 60 or score.overall_score < 60:
            concerns.append('Fit or experience evidence is below the shortlist threshold of 60.')
        credible = not concerns
        sponsorship = any(flag.code.value == 'SPONSORSHIP_UNKNOWN' for flag in score.review_flags)
        reason = (f'{"LA" if location == 0 else "US remote"}; fit {score.overall_score}/100, '
                  f'experience fit {score.dimensions.experience_level}/100 against {profile.resume_name or profile.profile_id}; '
                  f'{years} years verified employment.')
        if sponsorship:
            reason += ' Sponsorship requires confirmation.'
        candidates.append((penalty, not credible, location, -score.overall_score, job.id, {
            'id': job.id, 'title': job.title, 'company': job.company, 'url': job.url,
            'resume': profile.resume_name or profile.profile_id, 'reason': reason,
            'credible': credible, 'concerns': concerns, 'sponsorship_review': sponsorship,
        }))
    candidates.sort(key=lambda item: item[:5])
    reviewed = [item[-1] for item in candidates[:10]]
    return {'reviewed': reviewed, 'matches': [item for item in reviewed if item['credible']][:5],
            'candidate_count': len(candidates),
            'note': 'Automatically reviews up to 10 saved LA/US-remote candidates. Shows up to five matches; never fills with weak fits. Employment duration is not proof of skill-specific experience. Based on saved scores and last-known availability; confirm the posting before applying.'}
