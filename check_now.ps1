# check_now.ps1
# One-click "check everything now" script:
#   1. (optional) Triggers the GitHub Actions cloud run via API, if a
#      GITHUB_PAT is configured in .env, and waits for it to finish
#   2. Pulls the latest results (from the cloud run, whenever it
#      finished - just scheduled or just triggered above)
#   3. Runs the local part of the bot (tenders.kg, procurement.kg)
#   4. Opens a single combined report with all 5 sources in the browser
#
# This is meant for manual use (double-click check_now.bat). The
# scheduled Task Scheduler job keeps using run_local.ps1 directly,
# without opening a browser automatically every day.

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# Load .env manually here too (python-dotenv only affects the Python
# process; we need GITHUB_PAT inside PowerShell itself for the API call).
$EnvFile = Join-Path $ScriptDir ".env"
$envVars = @{}
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $envVars[$matches[1]] = $matches[2].Trim()
        }
    }
}

$githubPat = $envVars["GITHUB_PAT"]
$githubRepo = if ($envVars["GITHUB_REPO"]) { $envVars["GITHUB_REPO"] } else { "mrboikonov/Tenderbot" }
$workflowFile = "tender_bot.yml"

if ($githubPat) {
    Write-Host "Triggering cloud workflow run via GitHub API..."
    $headers = @{
        Authorization = "Bearer $githubPat"
        Accept        = "application/vnd.github+json"
    }
    $dispatchUrl = "https://api.github.com/repos/$githubRepo/actions/workflows/$workflowFile/dispatches"
    try {
        Invoke-RestMethod -Uri $dispatchUrl -Method Post -Headers $headers -Body '{"ref":"main"}' -ContentType "application/json"
        Write-Host "Cloud run triggered. Waiting for it to finish (up to 2 minutes)..."

        $runsUrl = "https://api.github.com/repos/$githubRepo/actions/workflows/$workflowFile/runs?per_page=1"
        $finished = $false
        for ($i = 0; $i -lt 24; $i++) {
            Start-Sleep -Seconds 5
            try {
                $runs = Invoke-RestMethod -Uri $runsUrl -Headers $headers
                $latestRun = $runs.workflow_runs[0]
                if ($latestRun.status -eq "completed") {
                    Write-Host "Cloud run finished with status: $($latestRun.conclusion)"
                    $finished = $true
                    break
                }
            } catch {
                # ignore transient API errors while polling
            }
        }
        if (-not $finished) {
            Write-Host "Cloud run did not finish within the wait window - continuing with whatever is available so far."
        }
    } catch {
        Write-Host "Could not trigger cloud workflow (check GITHUB_PAT permissions). Continuing with local part only."
    }
} else {
    Write-Host "GITHUB_PAT not set in .env - skipping cloud trigger, using latest scheduled cloud results."
}

Write-Host "Running run_local.ps1 (pull, bot, push)..."
powershell -ExecutionPolicy Bypass -File (Join-Path $ScriptDir "run_local.ps1")

Write-Host "Pulling any further cloud updates..."
git pull 2>$null 1>$null

Write-Host "Opening combined report..."
python (Join-Path $ScriptDir "generate_report.py")
