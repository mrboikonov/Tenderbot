# run_local.ps1
# Локальный запуск бота для площадок, заблокированных для GitHub Actions
# (tenders.kg, procurement.kg). Настраивается в Windows Task Scheduler,
# см. инструкцию в README.md -> "Локальный запуск для tenders.kg/procurement.kg".

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$LogFile = Join-Path $ScriptDir "run_local.log"

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Write-Output $line
    Add-Content -Path $LogFile -Value $line
}

Write-Log "=== Запуск run_local.ps1 ==="

try {
    Write-Log "git pull --rebase..."
    git pull --rebase origin main 2>&1 | ForEach-Object { Write-Log $_ }

    Write-Log "Запуск python bot.py..."
    # ONLY_SOURCES читается из .env (см. .env.example) — ограничивает
    # обработку только tenders.kg и procurement.kg.
    python bot.py 2>&1 | ForEach-Object { Write-Log $_ }

    Write-Log "git add/commit/push..."
    git add tenders_db.json sent_tenders.json
    $staged = git diff --cached --quiet; $hasChanges = $LASTEXITCODE -ne 0
    if ($hasChanges) {
        git commit -m "chore: update tenders_db.json (local: tenders.kg/procurement.kg) [skip ci]"
        git pull --rebase origin main
        git push origin main
        Write-Log "Изменения закоммичены и отправлены."
    } else {
        Write-Log "Нет изменений в базе — коммит не нужен."
    }

    Write-Log "=== Готово ==="
}
catch {
    Write-Log "ОШИБКА: $_"
    exit 1
}
