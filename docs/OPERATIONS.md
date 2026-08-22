# Operating JobRadar

## Start and stop

Start the loopback service:

```powershell
cd E:\Workspace\jobradar
.\.venv\Scripts\Activate.ps1
uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

Stop it with `Ctrl+C`. The dashboard and extension cannot call the API while this process is stopped.

## Score a job from the browser

1. Start the backend and confirm `/health` returns `status: ok`.
2. Open a job posting in Chrome or Edge.
3. Open the JobRadar extension.
4. Confirm the API URL and profile ID in **Settings**.
5. Select **Score this job**.
6. Review hard flags, manual-review signals, score, and rationale.
7. Open `/dashboard/` to manage its lifecycle.

The extension cannot run on browser-internal pages. A site without structured data may require a parser update; extraction fails explicitly rather than inventing fields.

## Use the dashboard

Open `http://127.0.0.1:8000/dashboard/` while Uvicorn is running.

- Paste a public job-posting URL into **Quick score** to extract, score, and save it.
- Each job card shows **Use resume** with the selected resume variant.
- **New**, **Viewed**, **Applied**, and **Skipped** filter lifecycle states.
- Search matches title and company.
- **Mark viewed** and **Applied** persist an event in SQLite.
- **Skip** requires a reason.
- Re-scoring the same canonical URL refreshes its score without resetting its lifecycle state.

Some authenticated or bot-protected pages block backend extraction. Open those
pages in Chrome or Edge and use the JobRadar extension, which extracts the
rendered page before calling the same scoring API.

Data is stored at `DATABASE_PATH` and is intentionally ignored by Git.

## Run discovery safely

Dry run is the default and never sends email:

```powershell
.\scripts\run_discovery.ps1
```

Override queries or limit:

```powershell
python -m agent.discovery --query "backend engineer United States" --limit 10
```

The agent searches, canonicalizes URLs, skips known jobs, extracts facts, applies hard filters, scores remaining roles, persists results, and prints the digest.

Without `--query`, the runner uses the scheduled multi-source plan:

- Track A every 2 hours: configured Greenhouse, Lever, Ashby, and custom careers.
- Track B every 4 hours: SerpAPI `google_jobs` role/location matrix.
- Track C every 12 hours: Brave gaps including LinkedIn, Indeed, ZipRecruiter,
  Workday, SmartRecruiters, and priority companies.

Intervals are stored in SQLite, so the Windows task should still invoke the
runner every two hours. Force every configured source during validation with:

```powershell
.\scripts\run_discovery.ps1 -ForceAllTracks -Limit 60
```

The run fetches known postings and only rescores those whose normalized content
changed. Dry-run prevents email but still persists discovery and track state.
The result budget is shared fairly across every query. Model-scored work is
separately capped by `MAX_SCORING_JOBS_PER_RUN` (default 50); excess eligible
postings are printed as `deferred` and remain available for a later run.

## Send the digest

Delivery occurs only with the explicit flag:

```powershell
.\scripts\run_discovery.ps1 -SendEmail
```

For Gmail, configure `GMAIL_USERNAME` and an app password. For SendGrid, configure `SENDGRID_API_KEY` and a verified `SENDGRID_FROM_EMAIL`. Always run a dry run first.

Qualified discovery jobs remain pending in SQLite until email delivery succeeds. A provider failure therefore does not lose roles on the next deduplicated run. Empty emails are skipped unless `SEND_EMPTY_DIGEST=true`.

## Two-hour scheduling on Windows

The discovery agent does not require Uvicorn and should run as a short Task Scheduler job every two hours. This is more reliable than keeping a Python loop alive continuously. The runner uses a named mutex, so a slow run cannot overlap the next one.

Before creating the task, complete these gates:

```powershell
.\scripts\run_discovery.ps1
.\scripts\run_discovery.ps1 -SendEmail
```

The first command must complete as a dry run. The second must deliver a real test email. Then create the task:

1. Open **Task Scheduler** and select **Create Task**.
2. Name it `JobRadar Discovery - Every 2 Hours`.
3. Under **Security options**, select your Windows account. Start with **Run only when user is logged on** while validating the setup.
4. On **Triggers**, create a daily trigger starting at the desired first-run time.
5. Under **Advanced settings**, select **Repeat task every: 2 hours** for a duration of **1 day** and enable the trigger.
6. On **Actions**, choose **Start a program**.
7. Program: `powershell.exe`.
8. Arguments: `-NoProfile -ExecutionPolicy Bypass -File "E:\Workspace\jobradar\scripts\run_discovery.ps1" -SendEmail -Limit 60`.
9. Start in: `E:\Workspace\jobradar`.
10. On **Settings**, enable **Run task as soon as possible after a scheduled start is missed** and select **Do not start a new instance** when the task is already running.
11. Save the task, right-click it, choose **Run**, and confirm the Last Run Result is `0x0`.

At a two-hour cadence, JobRadar can send at most 12 non-empty digests per day. The recommended starting window is daytime only if API usage or inbox volume becomes noisy. Do not enable **Run whether user is logged on or not** until the task has succeeded repeatedly and you understand how Windows stores the account credential.

Review Task Scheduler history, search-provider quota, scoring-provider quota, and the digest recipient after the first scheduled day.

Each runner invocation appends output to `logs/jobradar-YYYY-MM-DD.log` and
rewrites both `logs/last_run_status.txt` and `logs/errors_in_last_run.txt`.
Any completely failed discovery track makes the command exit nonzero, while a
partially successful track keeps its individual errors in the daily log. These
runtime files are private and ignored by Git.

## Test and update

```powershell
pytest tests -v
pytest tests -m integration -v
python -m pip check
git status --short
```

Tests mock Gemini, Ollama, search services, and email delivery. Passing tests do not prove that real credentials, quotas, or third-party pages are currently available.
