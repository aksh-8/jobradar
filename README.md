# JobRadar

JobRadar is a personal job-search intelligence system for Akash Biswal. It combines a Chrome/Edge Manifest V3 extension that scores job postings in real time with a Python discovery agent that finds qualified roles, deduplicates them, and sends an actionable compiled email digest.

## Current delivery status

Step 11 is in progress: the ignored private catalog contains four READY resume variants, and setup, operation, troubleshooting, audit, two-hour scheduling, per-run scoring limits, and local failure monitoring are implemented. Remaining real-world gates are provider credentials, one live discovery/email test, and Task Scheduler registration.

## Architecture

| Component | Responsibility | Technology |
| --- | --- | --- |
| Browser extension | Extract job postings, request scores, and render actions | Chrome/Edge MV3, vanilla JavaScript |
| Web dashboard | Score public job links, review resume recommendations, and manage application lifecycle state | Responsive HTML, CSS, and vanilla JavaScript |
| Backend API | Validate requests, scan hard filters, score roles, and persist events | Python 3.12, FastAPI, Uvicorn |
| Scoring providers | Produce structured scoring with a local fallback | Gemini 3.6 Flash, Ollama Qwen 2.5 7B |
| Persistence | Store discovered, viewed, skipped, and applied roles | SQLite, aiosqlite |
| Discovery agent | Monitor ATS/company boards, Google Jobs, and public search gaps | Python, public ATS APIs, SerpAPI, Brave Search |
| Digest delivery | Compose outreach guidance and send scheduled results | Gmail SMTP or SendGrid |

I am using one shared Python scoring engine for the extension and discovery agent instead of duplicating scoring logic because both clients must apply identical sponsorship, compensation, and quality rules.

## Build order

1. Repository foundation: `README.md`, `.env.example`, `.gitignore`, and `requirements.txt`
2. Database schema and migrations: `backend/database.py`
3. Structured resume catalog: `backend/resume_store.py`
4. Rule-based red-flag scanner and unit tests: `backend/red_flag_scanner.py`
5. Gemini/Ollama scoring engine and unit tests
6. FastAPI backend, `/api/score`, and `/health`
7. Chrome/Edge extension: manifest, content parser, popup, and background worker
8. Standalone web dashboard for scores and application lifecycle management
9. Discovery, outreach, and email-digest agent
10. Integration tests
11. Final setup, operation, and troubleshooting documentation

## Quick start

```powershell
cd E:\Workspace\jobradar
.\.venv\Scripts\Activate.ps1
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
if (-not (Test-Path config\resume_profile.json)) {
    Copy-Item config\resume_profile.example.json config\resume_profile.json
}

uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://127.0.0.1:8000/dashboard/`. Paste a public job-posting URL into **Quick score**, or use the browser extension on the active page when a site blocks server-side extraction. Every saved role displays the recommended resume. The server terminal must remain running. Real scoring uses the READY private catalog and `RESUME_PROFILE_ID=auto` to select among General, Platform, AIAutomation, and FDE.

## Common commands

```powershell
# All unit and integration tests
pytest tests -v

# Cross-component integration tests only
pytest tests -m integration -v

# Discovery dry run; never sends email
python -m agent.discovery

# Explicit digest delivery
python -m agent.discovery --send-email
```

## Documentation

- [Windows setup](docs/SETUP.md)
- [Private resume profile](docs/PROFILE.md)
- [Daily operation](docs/OPERATIONS.md)
- [Architecture and decision policy](docs/ARCHITECTURE.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Original-prompt implementation audit](docs/AUDIT.md)

## Security and production notes

- Never commit `.env`, API keys, Gmail app passwords, SendGrid credentials, SQLite databases, or resume documents.
- The extension will call a loopback-only backend by default. Do not expose the development server publicly without authentication, TLS, restrictive CORS, rate limiting, and secret management.
- Job pages and search providers can change markup, access rules, and terms. Parsers will fail explicitly and explain which source needs attention.
- Sponsorship value `UNKNOWN` is normally a manual-review state, not an automatic rejection. Configured known-sponsor companies remain application-eligible when a posting is silent; explicit no-sponsorship, citizenship/US-Person, ITAR, clearance, defense, sub-50-person company, and sub-$140,000 base signals remain hard filters.
