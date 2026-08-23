# JobRadar architecture and decision policy

This document describes the as-built Step 11 system. It is intended both for
operators and for senior-engineering review. The code remains the source of
truth when this overview and an implementation detail differ.

## System boundary

JobRadar is a personal, local-first job-search intelligence system. It discovers
public job postings, reads the canonical description, selects one of four
verified resume variants, applies deterministic eligibility rules, requests a
structured fit assessment, stores an auditable record in SQLite, and prepares a
compiled digest. It does not submit applications or scrape authenticated user
sessions.

The browser extension and dashboard call the loopback FastAPI service. The
dashboard can submit a public posting URL for backend extraction, while the
extension handles pages whose rendered browser content is not publicly
fetchable. Both paths use the same resume selection and scoring engine.
Scheduled discovery is a short-lived command and does not require FastAPI to
remain running.

## End-to-end flow

1. Windows Task Scheduler invokes `scripts/run_discovery.ps1` every two hours.
2. Durable track state decides which configured sources are due.
3. Search and board adapters emit normalized candidate results.
4. Authoritative ATS payloads are consumed directly. Search-engine results are
   followed to their preferred company or ATS job-description URL.
5. The extractor requires title, company, and complete description. It also
   records location, workplace type, employment type, dates, salary, and
   explicit sponsorship language when available.
6. Explicitly closed postings and expired `validThrough` dates are rejected
   before scoring. A bounded scheduled audit refreshes saved open roles and
   records closure separately from application lifecycle state.
7. Postings without verified United States location evidence are rejected
   before deduplication, persistence, or model scoring.
8. Existing postings are matched by ATS identity, canonical URL, normalized
   company/title/location, and finally a fuzzy content fingerprint.
9. Unchanged postings only refresh `last_seen_at`; changed postings are rescored.
10. The automatic resume selector chooses General, Platform, AI/Automation, or
   FDE using verified terms from the private profile catalog.
11. Deterministic hard filters run before any model call.
12. A shared per-run scoring budget admits eligible new/changed postings and
    defers excess work for a later run.
13. Gemini returns a deterministic strict five-dimension assessment; Ollama is
    the fallback.
14. The application computes the weighted total and verdict.
15. Jobs, scoring evidence, lifecycle events, digest state, and track state are
    persisted in SQLite.
16. One text/HTML digest compiles new roles, outreach, follow-ups, and weekly
    skill gaps. Delivery occurs only with `--send-email`/`-SendEmail`.

## Discovery tracks

### Track A: priority company monitors, every two hours

Public Greenhouse, Lever, and Ashby adapters enumerate configured boards without
waiting for search-engine indexing. A generic public career-page adapter covers
custom systems such as Apple, Google, Microsoft, Amazon, and Meta. The generic
adapter follows job-like links; JavaScript-only career sites may still need a
dedicated adapter.

Source configuration uses pipe-delimited `key=Company` values:

```dotenv
GREENHOUSE_BOARDS="scaleai=Scale AI|gleanwork=Glean"
LEVER_BOARDS=
ASHBY_BOARDS="cohere=Cohere"
CUSTOM_CAREER_PAGES="Apple=https://jobs.apple.com/en-us/search"
```

ATS payloads are authoritative content and are normalized without refetching the
hosted HTML. Direct board rows are title-gated to the configured engineering
families. A board result may proceed to extraction with incomplete location
metadata, but it must produce verified US location evidence before persistence
or scoring. Custom career results are fetched at their individual posting URL.

### Track B: Google Jobs, every four hours

SerpAPI is called with `engine=google_jobs`. The adapter follows
`next_page_token`, extracts Google job IDs and locations, and selects an
application URL from `apply_options`. Company/ATS URLs outrank arbitrary sites,
which outrank LinkedIn/Indeed/ZipRecruiter aggregator URLs.

Five role families are crossed with six location queries. Sponsorship language
is deliberately absent from discovery queries; it is evaluated from the actual
posting instead.

Role families:

- Senior Software Engineer / Software Engineer III / Staff Software Engineer
- Platform / Infrastructure / Developer Experience Engineer
- SDET / Software Development Engineer in Test / Quality Automation Engineer
- AI Automation / Applied AI Engineer
- Forward Deployed Engineer / Forward Deployed Software Engineer

Location tiers:

- Los Angeles, California
- Greater Los Angeles, California
- Remote, United States
- California
- East Coast, United States
- United States

### Track C: Brave gaps, every twelve hours

Brave runs source-specific searches across LinkedIn, Indeed, ZipRecruiter,
Workday, SmartRecruiters, priority big-tech companies, and configured FDE
companies. LinkedIn is intentionally included here rather than accessed through
an authenticated scraper or nonexistent unrestricted job-seeker API.

Brave is a recall/backstop source. It is not treated as the authoritative job
record, and every result must still produce a readable job-description page.

## Durable track scheduling

The `discovery_track_runs` table stores the last start, completion, status, and
error for each track. The two-hour Windows task can therefore invoke one command
while JobRadar independently enforces two-, four-, and twelve-hour intervals.
Failed tracks are due again on the next invocation. `--force-all-tracks` bypasses
interval checks for validation.

The result limit is divided across every query, including the broad United
States tier, with any remainder assigned predictably. This prevents later
queries from being starved by earlier ones.

The PowerShell runner also uses a named mutex so two discovery processes cannot
overlap.

## Posting identity and change detection

JobRadar evaluates identity in this order:

1. Lowercased ATS name plus ATS posting ID.
2. Canonical URL after removing fragments and common tracking parameters.
3. SHA-256 of normalized company, title, and location.
4. A 64-bit SimHash of normalized title, company, location, and description,
   gated by normalized company, high title similarity, compatible location, and
   a bounded Hamming distance.

The exact normalized posting facts are also hashed. An identity match with the
same content hash is unchanged; a different hash is rescored. When multiple
sources describe one role, direct ATS URLs outrank company pages, Google Jobs,
ordinary search, and Brave for the stored application URL.

The fuzzy layer is intentionally conservative but remains heuristic. Same-title
requisitions at one company/location can still require an ATS ID to remain
separate, and radically rewritten descriptions can evade the fuzzy threshold.

## Sponsorship and eligibility policy

The full description—not the search query or snippet—controls eligibility.

Hard rejection occurs for explicit evidence of:

- No current or future visa/employment sponsorship.
- USC/green-card-only or U.S. Person restrictions.
- ITAR or incompatible export controls.
- Required/expected government security clearance.
- Explicit defense or military systems work.
- Primarily manual test execution.
- Known company size below 50.
- Known maximum annual USD base salary below $140,000.

Sponsorship silence normally produces a manual-review signal. For companies in
`KNOWN_SPONSOR_COMPANIES`, silence does not downgrade the role; any explicit
role-level restriction still wins. This allowlist expresses application policy,
not proof that a particular role will sponsor.

Unknown salary remains visible as a review flag but does not by itself prevent
an `APPLY+REFERRAL` verdict.

## Resume selection

The private catalog contains shared verified facts plus four targeted profiles.
Automatic selection groups aliases so synonyms cannot inflate a profile:

- Target role: 5
- Headline: 5
- Target company: 4
- Skill or alias: 3
- Keyword: 1

The selector chooses a resume; it does not decide whether the job is eligible.
The private catalog and original documents are excluded from Git.

## Fit scoring

The model returns strict JSON with five 0-100 dimensions. The application owns
the weights and computes the total:

```text
skills_match        35%
experience_level    25%
domain_relevance    20%
role_type           10%
compensation_signal 10%
```

The model must use only supplied verified resume facts. A malformed or failed
Gemini response falls back once to schema-constrained Ollama. If both fail, the
posting is not assigned an invented score.

Both providers run with temperature zero. Gemini uses Google's supported
`google-genai` SDK and defaults to `gemini-3.6-flash` to control per-posting
latency and quota use; `GEMINI_MODEL` remains configurable. When neither salary
boundary is known, application code replaces the model's `compensation_signal` with a
neutral score of 50. This intentionally contributes five neutral points rather
than allowing a model to infer compensation from missing data. Known salary
data retains the provider's evaluated compensation score.

Scheduled discovery shares `MAX_SCORING_JOBS_PER_RUN` across every due track
(default 50). Hard rejections are evaluated before this budget and do not
consume it. Eligible postings beyond the ceiling are reported as deferred and
left unpersisted so a later run can score them; no placeholder score is stored.

Verdicts:

- Any hard flag: `REJECTED / SKIP`, score 0.
- Unknown sponsorship outside the allowlist: `MANUAL_REVIEW / BORDERLINE`.
- No blocking review and weighted score at least 60: `QUALIFIED / APPLY+REFERRAL`.
- Otherwise: `BELOW_THRESHOLD / SKIP`.

## United States location policy

United States location eligibility is a deterministic gate. Discovery rejects
non-US and unverified-location postings before persistence or model scoring.
Dashboard results, pending digests, follow-ups, and weekly missing-skill reports
also apply the gate, so historical foreign rows can remain in SQLite without
appearing in current operator workflows.

Eligible roles are ordered without changing their visible fit score:

1. Los Angeles and Greater Los Angeles
2. Remote roles explicitly available in the United States
3. Other California roles
4. East Coast roles
5. The rest of the United States

The classifier uses normalized posting location, workplace type, and description
evidence. A generic `Remote` label alone is insufficient because it does not
prove that the role accepts applicants in the United States.

## Persistence and delivery

SQLite contains normalized `jobs`, immutable `job_events`, schema migrations,
durable discovery-track state, and independent job-availability timestamps.
Closed roles are hidden from dashboards, digests, outreach, follow-ups, and
skill summaries without overwriting application lifecycle state. Qualified
roles remain pending until the email provider succeeds, so a delivery failure
does not lose a deduplicated role. Application status schedules a follow-up
after the configured interval.

The availability audit checks a bounded, score-prioritized set of non-skipped
roles concurrently. `AVAILABILITY_CHECK_LIMIT` defaults to 20 and
`AVAILABILITY_CHECK_INTERVAL_HOURS` defaults to 12. Explicit page closure text
and expired structured dates are deterministic evidence; fetch failures do not
silently classify a role as closed.

Dry-run means no email; discovery and SQLite persistence still occur. Empty
digests are skipped unless `SEND_EMPTY_DIGEST=true`.

## Known limitations and review questions

- Generic custom-career monitoring cannot guarantee coverage on JavaScript-only
  sites; high-priority systems should receive dedicated adapters and fixtures.
- Direct board configuration is maintained manually and can become stale when a
  company changes ATS.
- Google Jobs is a third-party SerpAPI dependency and should be monitored for
  schema and quota changes.
- LinkedIn/Indeed/ZipRecruiter are discovered through public search results; the
  system does not claim complete or real-time coverage of those platforms.
- Company-size enrichment is not implemented, so that filter activates only
  when a caller supplies a verified size.
- Known-sponsor membership is an application heuristic, not role-level evidence.
- Location normalization is textual rather than geocoded; ambiguous or missing
  locations are intentionally excluded until US eligibility can be verified.
- LLM dimensions are validated but still judgment-based; calibration against
  labeled historical decisions would improve consistency.
- The scoring ceiling controls per-run usage but is not a provider billing or
  daily-quota ledger; provider dashboards remain the authority for quota usage.
- Fuzzy identity thresholds need observation against real duplicate and
  same-title/multiple-requisition examples.
