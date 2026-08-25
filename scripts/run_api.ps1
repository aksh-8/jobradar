param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$environmentPath = Join-Path $projectRoot ".env"
$profilePath = Join-Path $projectRoot "config\resume_profile.json"
$logDirectory = Join-Path $projectRoot "logs"
$logPath = Join-Path $logDirectory "api.log"

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

$mutex = [System.Threading.Mutex]::new($false, "Local\JobRadarApi")
$hasLock = $false
try {
    $hasLock = $mutex.WaitOne(0)
    if (-not $hasLock) {
        Write-Output "Another JobRadar API process is active; skipping duplicate startup."
        exit 0
    }

    Push-Location $projectRoot
    try {
        "[$(Get-Date -Format 'o')] Starting JobRadar API on 127.0.0.1:$Port." |
            Tee-Object -FilePath $logPath -Append
        # Uvicorn writes normal lifecycle messages to stderr. Windows
        # PowerShell can promote those records to terminating errors under
        # Stop, so rely on the native process exit code for failure instead.
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & $pythonPath -m uvicorn backend.main:app --host 127.0.0.1 --port $Port 2>&1 |
                Tee-Object -FilePath $logPath -Append
            $processExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        "[$(Get-Date -Format 'o')] JobRadar API exited with code $processExitCode." |
            Tee-Object -FilePath $logPath -Append
        exit $processExitCode
    }
    finally {
        Pop-Location
    }
}
catch {
    "[$(Get-Date -Format 'o')] JobRadar API runner failure: $($_.Exception.Message)" |
        Tee-Object -FilePath $logPath -Append
    throw
}
finally {
    if ($hasLock) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
