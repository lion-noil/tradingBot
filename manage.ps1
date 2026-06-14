# manage.ps1 - TradingBot unified management script
# Usage: .\manage.ps1 [start|stop|restart|status|logs] [service]
#
# Services:
#   Docker : signal-bybit, signal-mt5, executor-a1
#   Local  : executor-a2

param(
    [string]$Command = "status",
    [string]$Service = ""
)

$ROOT     = $PSScriptRoot
$PYTHON   = if ($env:EXECUTOR_A2_PYTHON) { $env:EXECUTOR_A2_PYTHON } `
            else { "$env:USERPROFILE\anaconda3\envs\executor-a2\python.exe" }
$PID_FILE = "$ROOT\.executor-a2.pid"
$LOG_OUT  = "$ROOT\logs\executor-a2.log"
$LOG_ERR  = "$ROOT\logs\executor-a2-err.log"

function Is-A2Running {
    if (-not (Test-Path $PID_FILE)) { return $false }
    $storedPid = Get-Content $PID_FILE -ErrorAction SilentlyContinue
    if (-not $storedPid) { return $false }
    return $null -ne (Get-Process -Id ([int]$storedPid) -ErrorAction SilentlyContinue)
}

function Start-A2 {
    if (Is-A2Running) {
        Write-Host "[executor-a2] already running" -ForegroundColor Yellow
        return
    }

    $envPath   = "$ROOT\.env"
    $accountId = ""
    if (Test-Path $envPath) {
        $matched = Select-String -Path $envPath -Pattern "^EXEC_A2_ACCOUNT_ID\s*=" | Select-Object -First 1
        if ($matched) { $accountId = ($matched.Line -split "=", 2)[1].Trim() }
    }

    New-Item -ItemType Directory -Path "$ROOT\logs" -Force | Out-Null

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName               = $PYTHON
    $psi.Arguments              = "-m app.local_executor"
    $psi.WorkingDirectory       = $ROOT
    $psi.UseShellExecute        = $false
    $psi.CreateNoWindow         = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.EnvironmentVariables["EXEC_ACCOUNT_ID"] = $accountId
    $psi.EnvironmentVariables["PYTHONPATH"]       = $ROOT

    # Inherit current env vars (for .env loaded values are not here, but EXEC_ACCOUNT_ID override matters)
    foreach ($key in [System.Environment]::GetEnvironmentVariables().Keys) {
        if (-not $psi.EnvironmentVariables.ContainsKey($key)) {
            $psi.EnvironmentVariables[$key] = [System.Environment]::GetEnvironmentVariable($key)
        }
    }

    $proc = [System.Diagnostics.Process]::Start($psi)
    $proc.Id | Out-File $PID_FILE -Encoding utf8

    # async log redirect
    $proc.BeginOutputReadLine()
    $proc.BeginErrorReadLine()
    Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived -Action {
        if ($Event.SourceEventArgs.Data) { Add-Content $LOG_OUT $Event.SourceEventArgs.Data }
    } | Out-Null
    Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action {
        if ($Event.SourceEventArgs.Data) { Add-Content $LOG_ERR $Event.SourceEventArgs.Data }
    } | Out-Null

    Write-Host "[executor-a2] started (PID: $($proc.Id), account: $accountId)" -ForegroundColor Green
}

function Stop-A2 {
    if (-not (Is-A2Running)) {
        Write-Host "[executor-a2] not running" -ForegroundColor Gray
        Remove-Item $PID_FILE -ErrorAction SilentlyContinue
        return
    }
    $storedPid = [int](Get-Content $PID_FILE)
    Stop-Process -Id $storedPid -Force -ErrorAction SilentlyContinue
    Remove-Item $PID_FILE -ErrorAction SilentlyContinue
    Write-Host "[executor-a2] stopped" -ForegroundColor Yellow
}

function Show-Status {
    Write-Host ""
    Write-Host "=======================================" -ForegroundColor Cyan
    Write-Host "  TradingBot Service Status" -ForegroundColor Cyan
    Write-Host "=======================================" -ForegroundColor Cyan

    Write-Host ""
    Write-Host "[ Docker ]" -ForegroundColor Blue
    $rows = docker compose -f "$ROOT\docker-compose.yml" ps --format "{{.Name}}|{{.Status}}" 2>$null
    if ($rows) {
        foreach ($row in ($rows -split "`n" | Where-Object { $_ -match "\|" })) {
            $parts  = $row -split "\|"
            $name   = ($parts[0] -replace "tradingbot-", "" -replace "-1$", "").Trim()
            $status = $parts[1].Trim()
            if ($status -match "^Up") {
                Write-Host ("  [OK] {0,-22} {1}" -f $name, $status) -ForegroundColor Green
            } else {
                Write-Host ("  [--] {0,-22} {1}" -f $name, $status) -ForegroundColor Red
            }
        }
    } else {
        Write-Host "  (no Docker services or Docker not running)" -ForegroundColor Gray
    }

    Write-Host ""
    Write-Host "[ Local (Windows) ]" -ForegroundColor Blue
    if (Is-A2Running) {
        $storedPid = [int](Get-Content $PID_FILE)
        $proc      = Get-Process -Id $storedPid -ErrorAction SilentlyContinue
        $mins      = if ($proc) { [math]::Round(((Get-Date) - $proc.StartTime).TotalMinutes, 0) } else { "?" }
        Write-Host ("  [OK] {0,-22} PID:{1}  uptime:{2}min" -f "executor-a2", $storedPid, $mins) -ForegroundColor Green
    } else {
        Write-Host ("  [--] {0,-22} stopped" -f "executor-a2") -ForegroundColor Red
    }

    Write-Host ""
    Write-Host "=======================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Commands:" -ForegroundColor DarkGray
    Write-Host "  .\manage.ps1 start [service|all]" -ForegroundColor DarkGray
    Write-Host "  .\manage.ps1 stop  [service|all]" -ForegroundColor DarkGray
    Write-Host "  .\manage.ps1 logs   <service>" -ForegroundColor DarkGray
    Write-Host ""
}

# ────────────────────────────────────────────────
# Command routing
# ────────────────────────────────────────────────

switch ($Command.ToLower()) {

    "start" {
        if ($Service -eq "" -or $Service -eq "all") {
            Write-Host ">> Starting Docker services..." -ForegroundColor Cyan
            docker compose -f "$ROOT\docker-compose.yml" up -d
            Start-A2
        } elseif ($Service -eq "executor-a2") {
            Start-A2
        } else {
            docker compose -f "$ROOT\docker-compose.yml" up -d $Service
        }
        Start-Sleep 3
        Show-Status
    }

    "stop" {
        if ($Service -eq "" -or $Service -eq "all") {
            Write-Host ">> Stopping Docker services..." -ForegroundColor Cyan
            docker compose -f "$ROOT\docker-compose.yml" down
            Stop-A2
        } elseif ($Service -eq "executor-a2") {
            Stop-A2
        } else {
            docker compose -f "$ROOT\docker-compose.yml" stop $Service
        }
        Show-Status
    }

    "restart" {
        if ($Service -eq "" -or $Service -eq "all") {
            docker compose -f "$ROOT\docker-compose.yml" restart
            Stop-A2; Start-Sleep 1; Start-A2
        } elseif ($Service -eq "executor-a2") {
            Stop-A2; Start-Sleep 1; Start-A2
        } else {
            docker compose -f "$ROOT\docker-compose.yml" restart $Service
        }
        Start-Sleep 3
        Show-Status
    }

    "logs" {
        if ($Service -eq "executor-a2") {
            if (Test-Path $LOG_OUT) { Get-Content $LOG_OUT -Tail 50 -Wait }
            else { Write-Host "No log file yet." -ForegroundColor Gray }
        } elseif ($Service -ne "") {
            docker compose -f "$ROOT\docker-compose.yml" logs -f --tail=50 $Service
        } else {
            docker compose -f "$ROOT\docker-compose.yml" logs -f --tail=20
        }
    }

    "status" {
        Show-Status
    }

    default {
        Write-Host "Usage: .\manage.ps1 [start|stop|restart|status|logs] [service]"
        Write-Host "Services: signal-bybit, signal-mt5, executor-a1, executor-a2, all"
    }
}
