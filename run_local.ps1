# run_local.ps1
# Local run for sites blocked from GitHub Actions (tenders.kg, procurement.kg).
# Configured in Windows Task Scheduler, see README.md.

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$LogFile = Join-Path $ScriptDir "run_local.log"

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Write-Output $line
    Add-Content -Path $LogFile -Value $line
}

Write-Log "=== run_local.ps1 started ==="

Write-Log "Committing any pending local changes before pull..."
git add tenders_db.json sent_tenders.json
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "chore: sync local state before pull [skip ci]" *>> $LogFile
    Write-Log "Pending changes committed."
} else {
    Write-Log "No pending changes."
}

Write-Log "git pull --rebase..."
git pull --rebase origin main *>> $LogFile
if ($LASTEXITCODE -ne 0) {
    Write-Log "ERROR: git pull --rebase failed (exit code $LASTEXITCODE). Aborting."
    git rebase --abort *>> $LogFile
    exit 1
}

Write-Log "Running python bot.py..."
python bot.py *>> $LogFile
if ($LASTEXITCODE -ne 0) {
    Write-Log "ERROR: bot.py failed (exit code $LASTEXITCODE)."
}

Write-Log "git add/commit/push..."
git add tenders_db.json sent_tenders.json
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "chore: update tenders_db.json (local: tenders.kg/procurement.kg) [skip ci]" *>> $LogFile
    git pull --rebase origin main *>> $LogFile
    git push origin main *>> $LogFile
    Write-Log "Changes committed and pushed."
} else {
    Write-Log "No changes in database - commit not needed."
}

Write-Log "=== Done ==="
