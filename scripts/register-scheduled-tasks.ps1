# Register Windows Task Scheduler jobs for OHLCV EOD + Kite session tray.
# Requires: machine clock in IST (task times are wall-clock local).
# Run from an elevated or normal PowerShell (tasks run only when user is logged on):
#   powershell -ExecutionPolicy Bypass -File scripts\register-scheduled-tasks.ps1

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Scripts = Join-Path $RepoRoot "scripts"

function Get-UvPath {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
        (Join-Path $env:USERPROFILE ".cargo\bin\uv.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\uv\uv.exe")
    )
    foreach ($p in $candidates) {
        if (Test-Path $p) { return $p }
    }
    throw "uv.exe not found on PATH. Install uv or add it to PATH before registering tasks."
}

$Uv = Get-UvPath
Write-Host "Repo: $RepoRoot"
Write-Host "uv:   $Uv"

function Register-CmdTask {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$CmdFile,
        [Parameter(Mandatory = $true)]$Trigger,
        [string]$Description = ""
    )
    $cmdPath = Join-Path $Scripts $CmdFile
    if (-not (Test-Path $cmdPath)) {
        throw "Missing $cmdPath"
    }
    $action = New-ScheduledTaskAction `
        -Execute "cmd.exe" `
        -Argument "/c `"$cmdPath`"" `
        -WorkingDirectory $RepoRoot
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours 6)
    # Interactive user session so .env/ and tray/desktop work.
    $principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel Limited
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $Trigger `
        -Settings $settings `
        -Principal $principal `
        -Description $Description `
        -Force | Out-Null
    Write-Host "Registered $TaskName -> $CmdFile"
}

# Weekday EOD (Mon-Fri). Times are local; keep OS timezone = IST.
$nfoTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "16:10"
$cdsTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "17:30"
# MCX ready is 00:00 next calendar day after session; run shortly after.
$mcxTrigger = New-ScheduledTaskTrigger -Daily -At "00:05"
$trayTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

Register-CmdTask -TaskName "OhlcvEodNfo" -CmdFile "eod-nfo.cmd" -Trigger $nfoTrigger `
    -Description "NFO daily+fetch after 16:10 IST (session_end 15:40 + 30m). Logs under data/ohlcv/_eod/."
Register-CmdTask -TaskName "OhlcvEodCds" -CmdFile "eod-cds.cmd" -Trigger $cdsTrigger `
    -Description "CDS daily+fetch after 17:30 IST (session_end 17:00 + 30m)."
Register-CmdTask -TaskName "OhlcvEodMcx" -CmdFile "eod-mcx.cmd" -Trigger $mcxTrigger `
    -Description "MCX daily+fetch after 00:05 IST (prior trade_date). Requires catalog synced while contracts were live."
Register-CmdTask -TaskName "KiteSessionTray" -CmdFile "sessiontray.cmd" -Trigger $trayTrigger `
    -Description "Tray icon: green=valid Kite token, red=expired/missing. Menu Login runs kitetools login."

Write-Host ""
Write-Host "Done. Morning login is still manual: uv run kitetools login"
Write-Host "Unregister: powershell -ExecutionPolicy Bypass -File scripts\unregister-scheduled-tasks.ps1"
