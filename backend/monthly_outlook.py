"""Local thirty-day profile feedback without additional model requests."""
from collections import Counter, defaultdict
from backend.database import database_connection
from backend.red_flag_scanner import is_internship
from backend.location_policy import is_us_based
from backend.scoring_engine import ScoringResult
from backend.outlook_plan import build_action_plan


async def monthly_outlook(database_path=None):
    async with database_connection(database_path) as connection:
        cursor = await connection.execute("""
            SELECT title, company, employment_type, location, workplace_type,
                   description, score_details FROM jobs
            WHERE score_details IS NOT NULL
              AND datetime(first_discovered_at) >= datetime('now', '-30 days')
        """)
        rows = await cursor.fetchall()
    gaps = Counter()
    policies = Counter()
    dimensions = defaultdict(list)
    examples = defaultdict(list)
    count = scored = 0
    for row in rows:
        if is_internship(row['title'], row['employment_type']) or not is_us_based(
            row['location'], row['workplace_type'], row['description']
        ):
            continue
        result = ScoringResult.model_validate_json(row['score_details'])
        count += 1
        policies.update(flag.code.value for flag in result.hard_flags + result.review_flags)
        if not result.dimensions:
            continue
        scored += 1
        for name, value in result.dimensions.model_dump().items():
            dimensions[name].append(value)
        for gap in set(item.casefold().strip() for item in result.missing_requirements if item.strip()):
            gaps[gap] += 1
            if len(examples[gap]) < 3:
                examples[gap].append(f"{row['company']} — {row['title']}")
    actions = []
    def is_structural(gap):
        return any(word in gap for word in (
            'years', 'degree', 'clearance', 'citizen', 'sponsor', 'phd', 'ph.d', 'master'
        ))
    ranked = gaps.most_common()
    selected = ([item for item in ranked if is_structural(item[0])][:2]
                + [item for item in ranked if not is_structural(item[0])][:3])
    for gap, frequency in selected:
        structural = is_structural(gap)
        actions.append({
            'gap': gap, 'roles': frequency, 'examples': examples[gap],
            'action': (
                'Review role targeting and existing evidence. This requirement may not be '
                'addressable in one month; do not claim qualifications you lack.' if structural else
                'Week 1: check whether existing work already proves this skill. '
                'Weeks 2–3: build one relevant, testable project or feature. '
                'Week 4: document the result and add a resume bullet only after completing the work.'
            ),
        })
    averages = {name: round(sum(values) / len(values), 1)
                for name, values in dimensions.items()}
    return {
        'days': 30, 'roles_reviewed': count, 'roles_scored': scored,
        'dimension_averages': averages,
        'plan': build_action_plan(gaps, examples, averages, scored),
        'policy_signals': dict(policies.most_common()), 'actions': actions,
        'note': 'Based on stored roles first discovered in the last 30 days. Missing requirements '
                'are model assessments, not verified weaknesses. Similar wording may be counted separately. '
                'Scores measure role fit, not callback probability. Closed and applied roles remain in this retrospective.',
    }
