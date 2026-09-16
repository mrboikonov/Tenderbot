# run_local.ps1
# Local run for sites blocked from GitHub Actions (tenders.kg, procurement.kg).
# Configured in Windows Task Scheduler, see README.md.
#
# Note: bot.py writes its own detailed log to bot_run.log (UTF-8, from
# Python's logging module directly) rather than relying on PowerShell
# to capture and redirect Python's console output - this avoids
# encoding/NativeCommandError issues that can happen when PowerShell
# runs unattended under Task Scheduler with no real console attached.

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# Never let git open an interactive editor (important for unattended
# Task Scheduler runs - an editor popup would hang forever with nobody
# there to close it). "true" is a no-op command that just exits 0,
# so git falls back to whatever default message it already prepared.
$env:GIT_EDITOR = "true"

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$LogFile = Join-Path $ScriptDir "run_local.log"

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Write-Output $line
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

Write-Log "=== run_local.ps1 started ==="

Write-Log "Committing any pending local changes before pull..."
git add tenders_db.json sent_tenders.json 2>$null
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "chore: sync local state before pull [skip ci]" 2>$null 1>$null
    Write-Log "Pending changes committed."
} else {
    Write-Log "No pending changes."
}

Write-Log "git pull --rebase..."
git pull --rebase origin main 2>$null 1>$null
if ($LASTEXITCODE -ne 0) {
    Write-Log "git pull --rebase failed, aborting rebase and trying regular merge pull instead..."
    git rebase --abort 2>$null 1>$null
    git pull origin main --no-edit 2>$null 1>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Log "ERROR: git pull also failed (exit code $LASTEXITCODE). Manual intervention needed."
        exit 1
    }
}
Write-Log "Pull done."

Write-Log "Running python bot.py (see bot_run.log for detailed Python output)..."
python bot.py
Write-Log "python bot.py finished with exit code $LASTEXITCODE."

Write-Log "git add/commit/push..."
git add tenders_db.json sent_tenders.json 2>$null
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "chore: update tenders_db.json (local: tenders.kg/procurement.kg) [skip ci]" 2>$null 1>$null
    git pull --rebase origin main 2>$null 1>$null
    if ($LASTEXITCODE -ne 0) {
        git rebase --abort 2>$null 1>$null
        git pull origin main --no-edit 2>$null 1>$null
    }
    git push origin main 2>$null 1>$null
    Write-Log "Changes committed and pushed."
} else {
    Write-Log "No changes in database - commit not needed."
}

Write-Log "=== Done ==="
