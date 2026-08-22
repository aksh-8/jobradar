# JobRadar troubleshooting

## Dashboard shows `ERR_CONNECTION_REFUSED`

The backend is not running or is listening on another address. Start it and keep the terminal open:

```powershell
uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

Then reload `http://127.0.0.1:8000/dashboard/`.

## Port 8000 is already in use

Find the listener:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen
```

Stop the known process or use another port. If the port changes, update the extension API URL and its matching host permission in `extension/manifest.json`.

## Profile is still a draft

Confirm `.env` contains the correct `RESUME_PROFILE_PATH` and `RESUME_PROFILE_ID`. Validate the JSON using the command in [PROFILE.md](PROFILE.md), change only verified content to `ready`, and restart Uvicorn.

## Gemini fails and Ollama also fails

- Confirm `GEMINI_API_KEY` is not a placeholder and the configured model is available to that key.
- Confirm Ollama is running at `OLLAMA_BASE_URL`.
- Confirm the configured Ollama model is installed.
- Inspect the API error; it includes both provider failure reasons without exposing keys.

Hard-filtered jobs do not call either AI provider.

## Extension cannot extract a posting

- Confirm the active tab is an HTTP or HTTPS job-detail page.
- Reload the extension from the browser extensions page after source changes.
- Prefer the employer's canonical posting page over a search-results page.
- If the site changed markup and has no usable JobPosting JSON-LD, add a narrowly scoped selector and parser test.

## Extension reports an API error

- Open `/health` directly.
- Confirm the extension API setting is `http://127.0.0.1:8000` or `http://localhost:8000`.
- Restart Uvicorn after `.env` or resume-profile changes.
- A `409` response means the selected profile is still a draft.

## Discovery returns no opportunities

Check the printed summary separately for duplicates, extraction failures, hard rejections, below-threshold results, and provider errors. No results can be a valid outcome.

Verify `JOB_SEARCH_PROVIDER`, its API key, and the pipe-separated `JOB_SEARCH_QUERIES`. Search APIs return result metadata; JobRadar still needs access to each linked job page.

For scheduled discovery, inspect the `tracks_run`, `failed_tracks`, and
`deferred` fields in console output. Track A direct public ATS feeds can operate
without a search API key. Track B requires SerpAPI; Track C requires Brave. A
nonzero `deferred` count means the `MAX_SCORING_JOBS_PER_RUN` safety ceiling was
reached, not that those roles were rejected.

## Email is not sent

Email is intentionally disabled unless `--send-email` is present.

- Confirm `DIGEST_RECIPIENT_EMAIL`.
- Gmail requires an app password rather than the normal account password.
- SendGrid requires an API key and verified sender.
- Run without `--send-email` to confirm discovery and digest generation independently.
- Empty digests are skipped by default. Set `SEND_EMPTY_DIGEST=true` only when an empty confirmation email is genuinely useful.
- Qualified roles remain pending until successful delivery; rerunning after fixing credentials compiles them into the next email.

## The two-hour task does not run

- Run `.\scripts\run_discovery.ps1` manually from the repository first.
- Run `.\scripts\run_discovery.ps1 -SendEmail` and confirm a real email before debugging Task Scheduler.
- Confirm the task action uses `powershell.exe`, the quoted absolute script path, and `E:\Workspace\jobradar` as **Start in**.
- Enable Task Scheduler history and inspect **Last Run Result**. `0x0` means success.
- Inspect `logs/last_run_status.txt`, `logs/errors_in_last_run.txt`, and the
  current `logs/jobradar-YYYY-MM-DD.log`. A completely failed discovery track
  now produces a nonzero process exit code so Task Scheduler cannot report it
  as a successful run.
- A message saying another discovery run is active is safe; the overlap guard intentionally skipped the second instance.
- Tasks configured for a logged-out account may not have access to the same profile, environment, mapped drives, or stored credentials. Validate with **Run only when user is logged on** first.

## Database problems

SQLite files live under `data/` by default. Do not delete a database containing application history without making a backup.

For a new empty development database, stop Uvicorn and point `DATABASE_PATH` to a new filename. Migrations run automatically at startup.

## Git reports dubious ownership

Trust only this exact repository:

```powershell
git config --global --add safe.directory E:/Workspace/jobradar
```

Do not use a wildcard safe-directory entry.
