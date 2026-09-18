from backend.outlook_plan import build_action_plan


def test_plan_groups_experience_and_prioritizes_immediate_actions():
    gaps = {'5+ years software engineering': 4, '8 years experience': 2,
            'Temporal orchestration': 2, 'vllm serving': 1}
    examples = {gap: ['Acme — Engineer'] for gap in gaps}
    plan = build_action_plan(gaps, examples, {'experience_level': 40}, 12)
    assert [item['id'] for item in plan] == [
        'experience', 'resume-evidence', 'portfolio', 'orchestration', 'serving']
    assert [item['rank'] for item in plan] == [1, 2, 3, 4, 5]
    assert plan[0]['examples'] == ['Acme — Engineer']
    assert '6 requirement mentions' in plan[0]['evidence']
    assert all(len(item['steps']) == 3 and item['deliverable'] for item in plan)


def test_no_data_does_not_invent_a_plan():
    assert build_action_plan({}, {}, {}, 0) == []


def test_no_technical_evidence_does_not_prescribe_specific_tools():
    plan = build_action_plan({}, {}, {}, 2)
    assert len(plan) == 5
    assert not {'serving', 'orchestration', 'experience'} & {item['id'] for item in plan}
