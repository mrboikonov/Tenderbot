# run_local.ps1
# Local run for sites blocked from GitHub Actions (tenders.kg, procurement.kg).
# Configured in Windows Task Scheduler, see README.md.

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$LogFile = Join-Path $ScriptDir "run_local.log"

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Write-Output $line
    Add-Content -Path $LogFile -Value $line
}

Write-Log "=== run_local.ps1 started ==="

try {
    Write-Log "Committing any pending local changes before pull..."
    git add tenders_db.json sent_tenders.json
    git diff --cached --quiet
    $hasPending = $LASTEXITCODE -ne 0
    if ($hasPending) {
        git commit -m "chore: sync local state before pull [skip ci]"
        Write-Log "Pending changes committed."
    } else {
        Write-Log "No pending changes."
    }

    Write-Log "git pull --rebase..."
    git pull --rebase origin main 2>&1 | ForEach-Object { Write-Log $_ }

    Write-Log "Running python bot.py..."
    python bot.py 2>&1 | ForEach-Object { Write-Log $_ }

    Write-Log "git add/commit/push..."
    git add tenders_db.json sent_tenders.json
    git diff --cached --quiet
    $hasChanges = $LASTEXITCODE -ne 0
    if ($hasChanges) {
        git commit -m "chore: update tenders_db.json (local: tenders.kg/procurement.kg) [skip ci]"
        git pull --rebase origin main
        git push origin main
        Write-Log "Changes committed and pushed."
    } else {
        Write-Log "No changes in database - commit not needed."
    }

    Write-Log "=== Done ==="
}
catch {
    Write-Log "ERROR: $_"
    exit 1
}
