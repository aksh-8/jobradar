# JobRadar

JobRadar is a personal job-search intelligence system for Akash Biswal. It will combine a Chrome/Edge Manifest V3 extension that scores job postings in real time with a Python discovery agent that finds qualified roles, deduplicates them, and sends an actionable daily email digest.

## Current delivery status

Step 9 of 11 is complete: the repository foundation, scoring clients, application dashboard, and daily discovery/outreach/digest agent are implemented. Application code is intentionally added only in the numbered build order below.

## Architecture

| Component | Responsibility | Technology |
| --- | --- | --- |
| Browser extension | Extract job postings, request scores, and render actions | Chrome/Edge MV3, vanilla JavaScript |
| Web dashboard | Review scored jobs and manage application lifecycle state | Responsive HTML, CSS, and vanilla JavaScript |
| Backend API | Validate requests, scan hard filters, score roles, and persist events | Python 3.12, FastAPI, Uvicorn |
| Scoring providers | Produce structured scoring with a local fallback | Gemini 2.5 Pro, Ollama Qwen 2.5 7B |
| Persistence | Store discovered, viewed, skipped, and applied roles | SQLite, aiosqlite |
| Discovery agent | Search, filter, deduplicate, score, and log current roles | Python, Brave Search or SerpAPI |
| Digest delivery | Compose outreach guidance and send daily results | Gmail SMTP or SendGrid |

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

## Absolute-zero Windows setup

The commands below use PowerShell and assume Python 3.12 and Node.js 20 are installed. Run them from a normal, non-administrator terminal.

### 1. Open the project folder

Codex is already working in the repository. VS Code is optional, but it is useful for browsing files and running commands:

```powershell
cd "C:\Users\EPDMFGP\Documents\Codex\2026-08-21\i-am-building-a-personal-job\jobradar"
code .
```

Expected result: VS Code opens with `README.md`, `.env.example`, `.gitignore`, and `requirements.txt` at the repository root. If `code` is not recognized, open VS Code manually and choose **File > Open Folder**, then select the absolute folder above.

### 2. Verify required tools

```powershell
py -3.12 --version
node --version
git --version
```

Expected output resembles:

```text
Python 3.12.x
v20.x.x
git version 2.x.x.windows.x
```

If Python or Node reports a different major version, install the required version before continuing. The browser extension has no Node build step, but Node 20 is retained as the project-wide JavaScript tooling baseline.

### 3. Create and activate a Python virtual environment

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
```

Expected result: the prompt begins with `(.venv)` and `python --version` prints `Python 3.12.x`.

If PowerShell blocks activation, allow scripts only for the current terminal and retry:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### 4. Install pinned dependencies

```powershell
python -m pip install -r requirements.txt
python -m pip check
```

Expected result: installation finishes with `Successfully installed ...`, and the validation command prints `No broken requirements found.`

### 5. Create local configuration

```powershell
Copy-Item .env.example .env
```

Expected result: a local `.env` file appears. Replace placeholder values with your credentials; never paste credentials into source files, commits, screenshots, or chat messages.

Required for real-time scoring:

- `GEMINI_API_KEY`: a valid Google AI API key with access to the configured Gemini model.
- `BRAVE_SEARCH_API_KEY` or `SERPAPI_API_KEY`: required later for discovery, depending on `JOB_SEARCH_PROVIDER`.
- Gmail app credentials or a SendGrid API key: required later only when sending a digest.

### 6. Confirm the environment is ready

```powershell
python -c "import aiosqlite, fastapi, google.generativeai, httpx; print('JobRadar dependencies OK')"
```

Expected output:

```text
JobRadar dependencies OK
```

The `/health` endpoint confirms server and database readiness and reports whether Gemini is configured without exposing or testing the secret. Provider authentication occurs on the first real scoring request; no real Gemini request is made by the test suite.

## Planned commands

These commands become available as their corresponding build steps are completed:

```powershell
# Run all tests
pytest tests/ -v

# Start the backend
uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload

# Run discovery without sending email
python -m agent.discovery

# Run discovery and send the digest
python -m agent.discovery --send-email
```

## Security and production notes

- Never commit `.env`, API keys, Gmail app passwords, SendGrid credentials, SQLite databases, or resume documents.
- The extension will call a loopback-only backend by default. Do not expose the development server publicly without authentication, TLS, restrictive CORS, rate limiting, and secret management.
- Job pages and search providers can change markup, access rules, and terms. Parsers will fail explicitly and explain which source needs attention.
- Sponsorship value `UNKNOWN` is a manual-review state, not an automatic rejection. Explicit no-sponsorship, ITAR, clearance, defense, sub-50-person company, and sub-$140,000 base signals are hard filters.

## Git workflow for Step 1

Stage only the four foundation files:

```powershell
git add README.md .env.example .gitignore requirements.txt
git commit -m "chore: initialize JobRadar project foundation"
```

Expected result: Git creates one root commit containing exactly the four Step 1 files. Run `git status --short`; it should print nothing.
