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

If a run previously remained active indefinitely during scoring, keep
`REQUEST_TIMEOUT_SECONDS` set to a positive value such as `30`. A timed-out
Gemini request now falls back to Ollama; if that also fails, the individual
posting is reported without freezing the scheduler.

## Extension cannot extract a posting

- Confirm the active tab is an HTTP or HTTPS job-detail page.
- Reload the extension from the browser extensions page after source changes.
- Prefer the employer's canonical posting page over a search-results page.
- If the site changed markup and has no usable JobPosting JSON-LD, add a narrowly scoped selector and parser test.

## Extension reports an API error

- Open `/health` directly.
- Confirm the extension API setting is loopback HTTP on the host PC, or the
  private Tailscale HTTPS origin on another device. Do not include `/dashboard/`.
- Restart Uvicorn after `.env` or resume-profile changes.
- A `409` response means the selected profile is still a draft.

On Apple Careers, the console may report that
`apple.com/search-services/suggestions/defaultlinks` was blocked by CORS. That
request belongs to Apple's global navigation and does not indicate a JobRadar
failure. Judge the extension by whether its popup returns a score.

## Cover letter generation fails

- Confirm `GEMINI_API_KEY` is configured and that `COVER_LETTER_MODEL` names a
  model available to the account. By default it uses `gemini-3.6-flash`, matching
  this deployment's scoring model.
- A 503 can mean Gemini twice returned text violating the no-header,
  no-sentence-starting-with-`I`, or four-sentences-per-paragraph policy.
- Cover letters are generated on demand and not cached; retrying consumes a new
  Gemini request.

## Discovery returns no opportunities

Check the printed summary separately for duplicates, extraction failures, hard rejections, below-threshold results, and provider errors. No results can be a valid outcome.

Verify `JOB_SEARCH_PROVIDER`, its API key, and the pipe-separated `JOB_SEARCH_QUERIES`. Search APIs return result metadata; JobRadar still needs access to each linked job page.

For scheduled discovery, inspect the `tracks_run`, `failed_tracks`, and
`deferred` fields in console output. Direct public ATS feeds can operate
without a search API key. Scheduled broad/Apple/rotating discovery requires
Brave; SerpAPI is disabled and not required. A
nonzero `deferred` count means the `MAX_SCORING_JOBS_PER_RUN` safety ceiling was
reached, not that those roles were rejected.

For missing Apple, Google, Microsoft, Amazon, Meta, NVIDIA, or Tesla results,
run `.\.venv\Scripts\python.exe -m scripts.check_priority_sources`. Confirm
that every priority employer also has a matching `CUSTOM_CAREER_PAGES` entry.
The Apple search is always scoped to `jobs.apple.com`; one other priority domain
rotates daily. These searches do not require location keywords, and every result
must still pass the downstream USA-only extraction policy.

The dashboard header reads `logs/last_run_status.txt`. **Discovery failed**
means the last scheduled command exited nonzero; inspect
`logs/errors_in_last_run.txt` and the current dated log before considering API
quota. A third-party job page returning 401/403/429 is not the same as Brave
rejecting the search request.

JobRadar is intentionally US-only. A posting is excluded when its normalized
location and description do not verify United States eligibility. `Remote` by
itself is ambiguous and is rejected; use a posting or canonical employer page
that states `United States`, `US`, a US state, or a US city/state location.

## A closed job still appears

Reload the dashboard after the next scheduled discovery run. JobRadar rejects
explicit closure banners and expired structured `validThrough` dates during
normal extraction, then rechecks a bounded set of saved roles every 12 hours by
default. Increase `AVAILABILITY_CHECK_LIMIT` cautiously if the backlog is large;
each check fetches a real job page. Pages that block access remain visible until
closure can be verified rather than being removed on a guess.

## A historical foreign job is still in SQLite

This is expected. The USA-only policy hides historical foreign records from the
dashboard and excludes them from future digests, follow-ups, and skill-gap
summaries. It does not delete audit history. New non-US or unverified-location
discoveries are rejected before persistence and scoring.

## Find people returns no contacts or an error

- Confirm `BRAVE_SEARCH_API_KEY` is configured and has available quota.
- A zero-result search is valid and is cached for `CONTACT_CACHE_DAYS`; use
  **Refresh results** only when you intentionally want another paid/API search.
- Public search indexes may not expose a recruiter or team member for a small
  company. Use the suggested manual LinkedIn company-people search instead.
- Treat every result as a lead, not verified identity data. Open it yourself and
  confirm current company, responsibility, and role relevance before outreach.
- JobRadar does not access authenticated LinkedIn pages or send messages.

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
