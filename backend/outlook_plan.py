"""Turn observed fit gaps into bounded, evidence-linked monthly activities."""
import re


def build_action_plan(gaps, examples, averages, scored):
    if not scored:
        return []
    groups = {}
    for gap, count in gaps.items():
        if re.search(r'\b\d+\+?\s+years?\b', gap):
            category = 'experience'
        elif any(term in gap for term in ('temporal', 'step functions', 'orchestration')):
            category = 'orchestration'
        elif any(term in gap for term in ('vllm', 'sglang', 'tensorrt', 'model serving')):
            category = 'serving'
        else:
            continue
        group = groups.setdefault(category, {'mentions': 0, 'examples': []})
        group['mentions'] += count
        for example in examples[gap]:
            if example not in group['examples']:
                group['examples'].append(example)

    def item(key, title, drawback, timing, effort, impact, steps, deliverable, evidence):
        return dict(id=key, title=title, drawback=drawback, timing=timing,
                    effort=effort, impact=impact, steps=steps, deliverable=deliverable,
                    evidence=evidence, examples=groups.get(key, {}).get('examples', [])[:3])

    plan = []
    if 'experience' in groups:
        plan.append(item('experience', 'Aim at the right level today',
            'Repeated experience requirements exceed what the selected resumes demonstrate.',
            'Today', '45–60 minutes', 'Improves application targeting immediately.',
            ['Open Your automatic shortlist on this page: JobRadar reviews up to 10 saved LA or US-remote candidates.',
             'Review the up-to-five matches, recommended resumes, and individual fit explanations.',
             'Confirm sponsorship and current availability on the posting, then apply. Applied or skipped roles leave the shortlist automatically.'],
            'JobRadar supplies the shortlist below; you confirm the posting and choose where to apply.',
            f"{groups['experience']['mentions']} requirement mentions; experience-level average "
            f"{averages.get('experience_level', 0)}/100. Mentions can overlap within one role."))
    plan.append(item('resume-evidence', 'Rewrite three bullets around ownership',
        'Your engineering ownership may be underexplained. Check the evidence before assuming a missing skill.',
        'Days 1–3', '2 hours', 'Makes existing experience easier to assess.',
        ['Choose three features or systems you personally built in your two most relevant jobs or projects.',
         'For each, write: problem → what you owned → technologies → verified outcome.',
         'Replace a responsibility-only bullet in the relevant resume variant. Use numbers only when you can verify them.'],
        'Three accurate, role-relevant resume bullets ready for your next application.',
        f"Skills-match average: {averages.get('skills_match', 0)}/100 across {scored} scored roles. "
        'This is an evidence audit suggestion, not proof that your current bullets are weak.'))
    plan.append(item('portfolio', 'Publish one JobRadar engineering case study',
        'Existing platform and AI work can be easier for reviewers to evaluate with a concise demonstration.',
        'Week 1', '3–4 hours', 'Adds inspectable evidence of work already completed.',
        ['Write a one-page case study explaining the problem, architecture, and your own contributions.',
         'Show scheduling, deduplication, failed-provider handling, and scoring tests with screenshots or a short demo.',
         'Provide sample data and reproducible setup; remove private resumes and credentials before publishing.'],
        'One portfolio link and one truthful project bullet on the relevant resume.',
        'Suggested evidence packaging for JobRadar; the scoring data does not establish whether you already have a portfolio.'))
    if 'orchestration' in groups:
        plan.append(item('orchestration', 'Build a recoverable Temporal workflow',
            'Workflow-orchestration experience is missing from some matched-role assessments.',
            'Weeks 2–3', '6–10 hours', 'Creates direct evidence for a named platform skill.',
            ['Build a local Temporal workflow: fetch mock jobs → deduplicate → mock-score → produce a digest.',
             'Simulate a failed fetch and a worker restart. Verify the workflow retries and resumes.',
             'Test that retries cannot deliver the same digest twice; document all three scenarios.'],
            'A runnable demo, three recovery tests, and a README. Add Temporal only after completing and understanding it.',
            f"{groups['orchestration']['mentions']} requirement mentions for orchestration tools."))
    if 'serving' in groups:
        plan.append(item('serving', 'Benchmark a small vLLM service',
            'Some AI infrastructure roles ask for model-serving experience not demonstrated by the selected resume.',
            'Week 4', '6–10 hours; suitable hardware required', 'Adds a specific AI infrastructure example.',
            ['Check available hardware and model requirements first. Set a spending limit before renting any GPU.',
             'Serve one suitable model with vLLM and test concurrency levels 1, 4, and 8.',
             'Record latency, throughput, hardware, and configuration. Explain one tradeoff; if hardware is impractical, finish the orchestration demo instead.'],
            'A reproducible benchmark report and service demo; no production-scale claims.',
            f"{groups['serving']['mentions']} requirement mentions for model serving; treat a small sample as exploratory."))
    # Fill remaining places with practical review activities, not invented weaknesses.
    fallbacks = [
        item('gap-validation', 'Validate one recurring skill gap',
             'Model assessments can miss evidence or overstate a job requirement.', 'Week 2', '90 minutes',
             'Prevents spending the month learning a skill you already demonstrate.',
             ['Open three target job descriptions and highlight their required skills.',
              'Map each requirement to a real work example, project, or an actual gap.',
              'Select one repeated gap to address; update your resume only where existing evidence supports it.'],
             'A three-role evidence matrix and one justified learning priority.',
             'Suggested review activity; validate automated assessments against the actual postings.'),
        item('application-review', 'Check whether the changes improve your targeting',
             'A fit score alone cannot explain recruiter response or application quality.', 'Week 4', '1 hour',
             'Shows which changes to keep next month.',
             ['Review five applications submitted using the updated resume and portfolio.',
              'Record role level, location, resume variant, outreach, and any response.',
              'Compare the requirements with your evidence and choose one adjustment for the next five applications.'],
             'A five-application review with one concrete targeting adjustment.',
             'Suggested measurement activity; no callback improvement is assumed.'),
        item('project-proof', 'Turn one completed feature into a short demonstration',
             'A resume claim is easier to evaluate when the underlying work can be inspected.',
             'Week 4', '2 hours', 'Gives a recruiter or referral contact a concrete example to review.',
             ['Choose one completed feature relevant to a shortlisted role.',
              'Record a two-minute walkthrough showing the problem, implementation, and a passing test.',
              'Attach the walkthrough link to your portfolio and explain your individual contribution.'],
             'One accessible demo linked to a verified resume claim.',
             'Suggested evidence-building activity, not a measured deficiency.'),
    ]
    plan.extend(fallbacks[:max(0, 5 - len(plan))])
    for rank, activity in enumerate(plan[:5], 1):
        activity['rank'] = rank
    return plan[:5]
