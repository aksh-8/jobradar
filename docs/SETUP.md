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
  the discovery tracks below.
- `RESUME_PROFILE_PATH=./config/resume_profile.json`.
- `RESUME_PROFILE_ID=auto` for four-variant selection.
- Email credentials only if digest delivery will be used.
- `GREENHOUSE_BOARDS`, `LEVER_BOARDS`, and `ASHBY_BOARDS` for verified direct
  ATS monitoring. Validate every slug against its public endpoint before adding
  it.
- `CUSTOM_CAREER_PAGES` for official domains used by Apple and rotating
  priority-company Brave searches. Generic direct polling stays disabled.
- `BRAVE_SEARCH_API_KEY` for public LinkedIn/Indeed/ZipRecruiter/Workday gaps,
  broad company-career discovery, Apple, and rotating priority coverage.
- No SerpAPI key is required. `ENABLE_SERPAPI_DISCOVERY` remains `false` in the
  free deployment.
- `COVER_LETTER_MODEL=gemini-3.6-flash` controls on-demand cover letters and uses
  the existing `GEMINI_API_KEY`; no additional credential is required.
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

On a MacBook or other Tailscale device, clone or copy the repository, load its
`extension` directory unpacked, open **Settings** in the popup, and enter the
private origin shown by `tailscale serve status`, for example
`https://jobradar-host.example.ts.net`. Approve the exact-host permission. Do
not include `/dashboard/` in the API URL.

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

For private access from your other devices and automatic API startup, follow
**Private access with Tailscale Serve** in [OPERATIONS.md](OPERATIONS.md). Keep
Uvicorn bound to `127.0.0.1`; Tailscale Serve is the only remote entry point.

See [ARCHITECTURE.md](ARCHITECTURE.md) for track intervals, identity precedence,
and the exact scoring and sponsorship policy.
