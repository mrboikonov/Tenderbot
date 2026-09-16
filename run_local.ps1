# run_local.ps1
# Local run for sites blocked from GitHub Actions (tenders.kg, procurement.kg).
# Configured in Windows Task Scheduler, see README.md.

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# Never let git open an interactive editor (important for unattended
# Task Scheduler runs - an editor popup would hang forever with nobody
# there to close it). "true" is a no-op command that just exits 0,
# so git falls back to whatever default message it already prepared.
$env:GIT_EDITOR = "true"

# Force Python to use UTF-8 for stdout/stderr regardless of the console
# codepage Task Scheduler happens to run under (which can differ from
# an interactive terminal and otherwise garbles Cyrillic log text).
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

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
    Write-Log "git pull --rebase failed (exit code $LASTEXITCODE), aborting rebase and trying regular merge pull instead..."
    git rebase --abort *>> $LogFile
    git pull origin main --no-edit *>> $LogFile
    if ($LASTEXITCODE -ne 0) {
        Write-Log "ERROR: git pull also failed (exit code $LASTEXITCODE). Manual intervention needed."
        exit 1
    }
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
    if ($LASTEXITCODE -ne 0) {
        git rebase --abort *>> $LogFile
        git pull origin main --no-edit *>> $LogFile
    }
    git push origin main *>> $LogFile
    Write-Log "Changes committed and pushed."
} else {
    Write-Log "No changes in database - commit not needed."
}

Write-Log "=== Done ==="
