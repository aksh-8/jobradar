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

`ScoringResult.skills_matched` is computed in application code from the selected
verified resume and posting. Outreach never interpolates the model's raw JD
requirement text. Older stored scores without this field fall back to verified
capabilities from their recorded resume variant.

Cover-letter generation is separate from scoring and runs only after an explicit
user action. `POST /api/cover-letter` accepts either a stored job ID or the
posting/score pair held by the extension, resolves the recorded READY resume,
and calls `COVER_LETTER_MODEL` with temperature zero. The default inherits the
existing Gemini scoring model so cover letters do not silently opt into a paid
Pro tier. Its plain-text response is not persisted.

## Discovery tracks

### Tier 1: verified direct ATS monitors, every two hours

Public Greenhouse, Lever, and Ashby adapters enumerate configured boards without
waiting for search-engine indexing. Only slugs validated against the live public
ATS endpoint belong in these mappings; a company name is not an ATS slug.

Source configuration uses pipe-delimited `key=Company` values:

```dotenv
GREENHOUSE_BOARDS="scaleai=Scale AI|gleanwork=Glean|affirm=Affirm|anthropic=Anthropic|block=Block|cloudflare=Cloudflare|databricks=Databricks|figma=Figma|flexport=Flexport|lyft=Lyft|pagerduty=PagerDuty|singlestore=SingleStore|verkada=Verkada|zscaler=Zscaler"
LEVER_BOARDS=
ASHBY_BOARDS="cohere=Cohere|linear=Linear"
CUSTOM_CAREER_PAGES="Apple=https://jobs.apple.com/en-us/search"
ENABLE_CUSTOM_CAREER_POLLING=false
```

ATS payloads are authoritative content and are normalized without refetching the
hosted HTML. Direct board rows are title-gated to the configured engineering
families. A board result may proceed to extraction with incomplete location
metadata, but it must produce verified US location evidence before persistence
or scoring. Generic custom-page polling is disabled by default because most
large proprietary career sites render with JavaScript. `CUSTOM_CAREER_PAGES`
still supplies authoritative domains for scoped Brave searches.

### Tier 2: bounded Brave coverage, every twelve hours

One cycle uses exactly nine base Brave requests:

1. Five nationwide role-family searches.
2. One combined Los Angeles/Greater Los Angeles search.
3. One combined US-remote search.
4. One fixed `site:jobs.apple.com` career search.
5. One official career-domain search for a non-Apple priority company, rotated
   deterministically once per calendar day.

The nationwide searches explicitly cover public LinkedIn, Indeed,
ZipRecruiter, Workday, and SmartRecruiters results while retaining a broad
`careers` alternative for proprietary company sites. JobRadar never logs into
or scrapes an authenticated LinkedIn session. Sponsorship terms are deliberately
absent from search queries; the actual description and company policy control
eligibility.

Role families:

- Senior Software Engineer / Software Engineer III / Staff Software Engineer
- Platform / Infrastructure / Developer Experience Engineer
- SDET / Software Development Engineer in Test / Quality Automation Engineer
- AI Automation / Applied AI Engineer
- Forward Deployed Engineer / Forward Deployed Software Engineer

At two cycles per day this is 18 scheduled Brave requests per day, or about 540
in a 30-day month. Direct ATS polling does not consume Brave requests. Contact
discovery and manual searches use additional requests and remain user-initiated.

Brave is a recall/backstop source. Scoped priority searches discard any result
outside the configured official host. Their direct company-career URLs outrank
legacy Google Jobs and aggregator links when a duplicate is refreshed. Apple
therefore opens on `jobs.apple.com` whenever that canonical posting is found.
The Apple adapter reads the official page's embedded router payload so the full
description, location, posting date, and compensation can be normalized without
a headless browser.

### Optional legacy SerpAPI compatibility

The Google Jobs adapters remain available for experiments, but scheduled
SerpAPI discovery is off by default and is not a required credential. It runs
only when both `ENABLE_SERPAPI_DISCOVERY=true` and a real `SERPAPI_API_KEY` are
configured. The production personal deployment uses direct ATS feeds and Brave.

## Durable track scheduling

The `discovery_track_runs` table stores the last start, completion, status, and
error for each track. The two-hour Windows task can therefore invoke one command
while JobRadar independently enforces two- and twelve-hour intervals (plus
four-hour intervals only if legacy SerpAPI discovery is deliberately enabled).
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
sources describe one role, direct ATS and scoped official career URLs outrank
legacy Google Jobs and aggregator pages for the stored application URL.

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
latency and quota use; `GEMINI_MODEL` remains configurable. Gemini SDK requests
and Ollama HTTP requests are both bounded by `REQUEST_TIMEOUT_SECONDS`, so a
stalled model call falls back or fails the posting instead of blocking the
entire scheduled run. When neither salary
boundary is known, application code replaces the model's `compensation_signal` with a
neutral score of 50. This intentionally contributes five neutral points rather
than allowing a model to infer compensation from missing data. Known salary
data retains the provider's evaluated compensation score.

Scheduled discovery shares `MAX_SCORING_JOBS_PER_RUN` across every due track
(default 50). Hard rejections are evaluated before this budget and do not
consume it. Eligible postings beyond the ceiling are reported as deferred and
left unpersisted so a later run can score them; no placeholder score is stored.
Results are processed round-robin across a track's queries, preventing the
first role family, location, or priority employer from consuming the entire
scoring budget.

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

Applied roles remain durable records, but their dashboard view is exclusive:
they appear only under **Applied** and are excluded from pending opportunity
digests and weekly missing-skill aggregation.

The availability audit checks a bounded, score-prioritized set of non-skipped
roles concurrently. `AVAILABILITY_CHECK_LIMIT` defaults to 20 and
`AVAILABILITY_CHECK_INTERVAL_HOURS` defaults to 12. Explicit page closure text
and expired structured dates are deterministic evidence; fetch failures do not
silently classify a role as closed.

Dry-run means no email; discovery and SQLite persistence still occur. Empty
digests are skipped unless `SEND_EMPTY_DIGEST=true`.

## Public contact discovery

Contact discovery is an explicit dashboard action, not part of scheduled job
discovery. For one selected role, Brave runs three bounded public searches:
technical recruiting, engineering management matched to the role family, and
company leadership/team pages. Public result titles, snippets, and URLs are
filtered for company evidence, classified, and ranked recruiter, hiring
manager, then relevant team member. The top results are cached in
`job_contacts`; `job_contact_searches` also caches an empty result so repeated
panel opens do not consume search quota.

LinkedIn results are search-engine links only. JobRadar never signs in to,
opens for extraction, scrapes, or sends messages through LinkedIn. Every result
is labeled as a lead requiring manual verification because titles and company
associations in search indexes can be stale.

## Known limitations and review questions

- JavaScript-only proprietary career sites other than Apple depend on Brave
  indexing until they receive a dedicated adapter and fixtures.
- Direct board configuration is maintained manually and can become stale when a
  company changes ATS.
- SerpAPI is optional legacy compatibility and remains disabled unless explicitly
  opted in; it is not part of the free production schedule.
- LinkedIn/Indeed/ZipRecruiter are discovered through public search results; the
  system does not claim complete or real-time coverage of those platforms.
- Contact suggestions inherit search-index staleness and can be incomplete or
  wrong; current employment and relevance must be confirmed manually.
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
