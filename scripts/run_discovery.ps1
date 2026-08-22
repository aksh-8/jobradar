param(
    [switch]$SendEmail,
    [switch]$ForceAllTracks,
    [ValidateRange(1, 500)]
    [int]$Limit = 60
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$environmentPath = Join-Path $projectRoot ".env"
$profilePath = Join-Path $projectRoot "config\resume_profile.json"
$logDirectory = Join-Path $projectRoot "logs"
$statusPath = Join-Path $logDirectory "last_run_status.txt"
$errorPath = Join-Path $logDirectory "errors_in_last_run.txt"
$dailyLogPath = Join-Path $logDirectory ("jobradar-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

if (-not (Test-Path -LiteralPath $logDirectory)) {
    New-Item -ItemType Directory -Path $logDirectory | Out-Null
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "JobRadar Python environment not found: $pythonPath"
}
if (-not (Test-Path -LiteralPath $environmentPath)) {
    throw "JobRadar environment file not found: $environmentPath"
}
if (-not (Test-Path -LiteralPath $profilePath)) {
    throw "JobRadar resume profile not found: $profilePath"
}

$mutex = [System.Threading.Mutex]::new($false, "Local\JobRadarDiscovery")
$hasLock = $false
try {
    $hasLock = $mutex.WaitOne(0)
    if (-not $hasLock) {
        Write-Output "Another JobRadar discovery run is active; skipping overlap."
        exit 0
    }

    Push-Location $projectRoot
    try {
        $arguments = @("-m", "agent.discovery", "--limit", $Limit)
        if ($SendEmail) {
            $arguments += "--send-email"
        }
        if ($ForceAllTracks) {
            $arguments += "--force-all-tracks"
        }
        $startedAt = Get-Date -Format "o"
        "[$startedAt] Starting JobRadar discovery." |
            Tee-Object -FilePath $dailyLogPath -Append
        # Windows PowerShell promotes native stderr records to terminating
        # errors under Stop. Preserve warnings in the log and use the native
        # process exit code as the actual success/failure signal.
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & $pythonPath @arguments 2>&1 |
                Tee-Object -FilePath $dailyLogPath -Append
            $processExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        $finishedAt = Get-Date -Format "o"
        $status = "[$finishedAt] Exit code: $processExitCode"
        Set-Content -LiteralPath $statusPath -Value $status
        $status | Tee-Object -FilePath $dailyLogPath -Append
        if ($processExitCode -ne 0) {
            Set-Content -LiteralPath $errorPath -Value $status
        }
        else {
            Set-Content -LiteralPath $errorPath -Value "No errors in the last run."
        }
        exit $processExitCode
    }
    finally {
        Pop-Location
    }
}
catch {
    $failure = "[$(Get-Date -Format 'o')] Runner failure: $($_.Exception.Message)"
    Set-Content -LiteralPath $statusPath -Value $failure
    Set-Content -LiteralPath $errorPath -Value $failure
    $failure | Tee-Object -FilePath $dailyLogPath -Append
    throw
}
finally {
    if ($hasLock) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
