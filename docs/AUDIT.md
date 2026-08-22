# Original-prompt implementation audit

This audit compares the current repository with the original JobRadar specification supplied by the project owner.

## Implemented and covered by automated tests

- FastAPI loopback service, startup SQLite migrations, health endpoint, and structured score API.
- Strict private catalog with four existing resume variants and automatic variant selection.
- Deterministic hard filters for explicit sponsorship restrictions, USC/GC and US Person requirements, ITAR/export controls, clearance, defense work, primarily manual test execution, known sub-50-person companies, and known sub-$140,000 salary ranges.
- Deterministic-temperature Gemini structured JSON scoring with Ollama fallback,
  the specified five dimensions and weights, and an application-owned neutral
  compensation score when salary data is absent.
- Chrome/Edge Manifest V3 extension that extracts, scores, displays the decision evidence, logs applications, skips roles, and drafts outreach.
- Three-track discovery with public Greenhouse/Lever/Ashby feeds, custom career
  pages, SerpAPI Google Jobs pagination, Brave gaps including LinkedIn/Indeed/
  ZipRecruiter, canonical posting extraction, layered cross-source identity,
  changed-posting rescoring, and SQLite persistence.
- Compiled text/HTML digest with resume selection, sponsorship, salary, outreach strategy, recruiter/referral/cold-email drafts, missing requirements, weekly skill gaps, and follow-up reminders.
- Gmail SMTP and SendGrid delivery guarded by the explicit `--send-email` or `-SendEmail` option.
- Pending-delivery tracking: roles are marked delivered only after the email provider succeeds.
- Overlap-safe PowerShell runner, durable per-track intervals, and documented
  two-hour Windows Task Scheduler operation.
- Unit and cross-component integration tests with external providers mocked.

## Partial or intentionally different

- The normalized `jobs` plus immutable `job_events` schema replaces the original single wide `application_events` table. It preserves cleaner deduplication and event history, but the dashboard currently exposes only discovered, viewed, skipped, and applied lifecycle states.
- JSON-LD, dedicated LinkedIn/Greenhouse/Lever/Workday selectors, a dynamic-page MutationObserver, and generic DOM extraction are implemented, but real sites can still change beyond the mocked parser fixtures.
- The extension uses a safe generic DOM fallback; Mozilla Readability is not bundled.
- Public ATS feeds and a generic custom-career adapter are present. JavaScript-only
  priority career sites may still require dedicated company-specific adapters.
- Outreach is evidence-based and copy-ready, but JobRadar does not discover private recruiter email addresses or personal contact data.
- Company size is enforced when known. Search and job pages often omit it, so unknown size still requires human review or a future enrichment source.
- Response types, interview stages, referral contacts, and a dashboard control for recording responses remain narrower than the original wide schema.

## Real-world readiness gates

- Add a real SerpAPI key for Google Jobs Track B and a real Brave Search key for
  gap-discovery Track C. Track A public ATS monitoring needs neither key.
- Add Gemini credentials for the supported `google-genai` adapter or verify the
  configured Ollama model.
- Add Gmail app-password or SendGrid credentials and complete one explicit email test.
- Register and observe the two-hour Windows scheduled task only after dry-run and delivery tests pass.
- Reload the unpacked extension and visually verify the expanded popup; local extension pages are not reachable through this session's browser security policy.
- Recheck third-party page parsers when job sites change their markup.
