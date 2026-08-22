# JobRadar Windows setup

Run all commands from a normal PowerShell terminal. Administrator privileges are not required.

## 1. Verify prerequisites

```powershell
cd E:\Workspace\jobradar
py -3.12 --version
git --version
```

Python must report version 3.12.x. Node.js 20 is needed only for JavaScript parser tests; the extension has no build step.

## 2. Create the Python environment

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip check
```

If activation is blocked, allow local scripts for only the current terminal:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## 3. Create private configuration

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
if (-not (Test-Path config\resume_profile.json)) {
    Copy-Item config\resume_profile.example.json config\resume_profile.json
}
```

Both destination files are ignored by Git. Keep API keys, app passwords, private resume facts, and databases out of commits and chat messages.

Edit `.env` and configure:

- `GEMINI_API_KEY` for primary scoring.
- `JOB_SEARCH_PROVIDER` only for manual `--query` runs; scheduled discovery uses
  the three tracks below.
- `RESUME_PROFILE_PATH=./config/resume_profile.json`.
- `RESUME_PROFILE_ID=auto` for four-variant selection.
- Email credentials only if digest delivery will be used.
- `GREENHOUSE_BOARDS`, `LEVER_BOARDS`, `ASHBY_BOARDS`, and
  `CUSTOM_CAREER_PAGES` for Track A priority-company monitoring.
- `SERPAPI_API_KEY` for Track B Google Jobs discovery.
- `BRAVE_SEARCH_API_KEY` for Track C LinkedIn/Indeed/ZipRecruiter and custom-site
  gap discovery.
- `MAX_SCORING_JOBS_PER_RUN` as the shared model-call safety ceiling (start with
  the default 50 and adjust only after observing provider usage).
- `PRIORITY_COMPANIES`, `FDE_TARGET_COMPANIES`, and
  `KNOWN_SPONSOR_COMPANIES` for company queries and sponsorship-silence policy.

Follow [PROFILE.md](PROFILE.md) before changing the profile status to `ready`.

## 4. Validate the installation

```powershell
python -m pip check
pytest tests -v
```

Expected result: no broken requirements and all tests passing without network access or credentials.

Validate that all configured resume variants are READY:

```powershell
python -c "from backend.resume_store import configured_resume_catalog; print([(p.resume_name,p.status.value) for p in configured_resume_catalog().list_profiles()])"
```

## 5. Install the browser extension

Chrome:

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Select **Load unpacked**.
4. Choose `E:\Workspace\jobradar\extension`.
5. Pin JobRadar from the extensions menu.

Edge:

1. Open `edge://extensions`.
2. Enable **Developer mode**.
3. Select **Load unpacked**.
4. Choose `E:\Workspace\jobradar\extension`.

The extension requests access only to the active tab after you invoke it and to the loopback JobRadar API.

## 6. Start JobRadar

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

Keep the terminal open. Verify:

- API: `http://127.0.0.1:8000/health`
- Dashboard: `http://127.0.0.1:8000/dashboard/`
- API schema: `http://127.0.0.1:8000/docs`

The health endpoint checks server/database readiness and whether Gemini appears configured. It does not make a paid provider request.

See [ARCHITECTURE.md](ARCHITECTURE.md) for track intervals, identity precedence,
and the exact scoring and sponsorship policy.
