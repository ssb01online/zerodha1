# Register Windows Task Scheduler jobs for OHLCV EOD + Kite session tray.
# Ready times are defined in IST; this script converts them to the OS local
# timezone (e.g. W. Europe / Stockholm). Re-run after a DST change so local
# clock times stay aligned with IST.
# Tasks run only when the user is logged on:
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

function Get-IndiaTimeZone {
    return [System.TimeZoneInfo]::FindSystemTimeZoneById("India Standard Time")
}

function Convert-IstWallClockToLocal {
    <#
    .SYNOPSIS
      Map an IST HH:mm wall clock to the equivalent local DateTime (current DST rules).
    #>
    param(
        [Parameter(Mandatory = $true)][string]$IstHm
    )
    if ($IstHm -notmatch '^\d{1,2}:\d{2}$') {
        throw "Expected HH:mm IST time, got '$IstHm'"
    }
    $hour, $minute = ($IstHm -split ':') | ForEach-Object { [int]$_ }
    $istTz = Get-IndiaTimeZone
    $localTz = [System.TimeZoneInfo]::Local
    $istNow = [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $istTz)
    # Unspecified Kind = wall clock in $istTz (Get-Date would be Local and break ConvertTimeToUtc).
    $istDt = [DateTime]::new(
        $istNow.Year, $istNow.Month, $istNow.Day, $hour, $minute, 0,
        [DateTimeKind]::Unspecified
    )
    $utc = [System.TimeZoneInfo]::ConvertTimeToUtc($istDt, $istTz)
    return [System.TimeZoneInfo]::ConvertTimeFromUtc($utc, $localTz)
}

function Get-IstLocalDayDelta {
    param([Parameter(Mandatory = $true)][string]$IstHm)
    $localDt = Convert-IstWallClockToLocal $IstHm
    $istTz = Get-IndiaTimeZone
    $hour, $minute = ($IstHm -split ':') | ForEach-Object { [int]$_ }
    $istNow = [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $istTz)
    $istDt = [DateTime]::new(
        $istNow.Year, $istNow.Month, $istNow.Day, $hour, $minute, 0,
        [DateTimeKind]::Unspecified
    )
    return ($localDt.Date - $istDt.Date).Days
}

function Format-LocalHm {
    param([DateTime]$LocalDt)
    return $LocalDt.ToString("HH:mm")
}

function Shift-Weekdays {
    <#
    .SYNOPSIS
      Shift Mon–Fri (etc.) by the calendar-day delta between IST and local for one fire.
    #>
    param(
        [Parameter(Mandatory = $true)][DayOfWeek[]]$Days,
        [Parameter(Mandatory = $true)][int]$DayDelta
    )
    if ($DayDelta -eq 0) { return $Days }
    $all = [DayOfWeek]::Sunday, [DayOfWeek]::Monday, [DayOfWeek]::Tuesday,
        [DayOfWeek]::Wednesday, [DayOfWeek]::Thursday, [DayOfWeek]::Friday,
        [DayOfWeek]::Saturday
    $shifted = foreach ($d in $Days) {
        $all[(([int]$d + $DayDelta) % 7 + 7) % 7]
    }
    return ,$shifted
}

$Uv = Get-UvPath
$localTz = [System.TimeZoneInfo]::Local
$istTz = Get-IndiaTimeZone
Write-Host "Repo:      $RepoRoot"
Write-Host "uv:        $Uv"
Write-Host "Local TZ:  $($localTz.Id) ($($localTz.DisplayName))"
Write-Host "IST TZ:    $($istTz.Id)"

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

# Ready times in IST (download_ready). Converted to local wall clock below.
$nfoIst = "16:10"
$cdsIst = "17:30"
$mcxIst = "00:05"

$nfoLocal = Convert-IstWallClockToLocal $nfoIst
$cdsLocal = Convert-IstWallClockToLocal $cdsIst
$mcxLocal = Convert-IstWallClockToLocal $mcxIst

$weekdayIst = @(
    [DayOfWeek]::Monday, [DayOfWeek]::Tuesday, [DayOfWeek]::Wednesday,
    [DayOfWeek]::Thursday, [DayOfWeek]::Friday
)
$nfoDays = Shift-Weekdays -Days $weekdayIst -DayDelta (Get-IstLocalDayDelta $nfoIst)
$cdsDays = Shift-Weekdays -Days $weekdayIst -DayDelta (Get-IstLocalDayDelta $cdsIst)

$nfoHm = Format-LocalHm $nfoLocal
$cdsHm = Format-LocalHm $cdsLocal
$mcxHm = Format-LocalHm $mcxLocal

Write-Host ""
Write-Host "Trigger map (IST -> local $($localTz.Id)):"
Write-Host "  NFO  $nfoIst IST -> $nfoHm local  days=$($nfoDays -join ',')"
Write-Host "  CDS  $cdsIst IST -> $cdsHm local  days=$($cdsDays -join ',')"
Write-Host "  MCX  $mcxIst IST -> $mcxHm local  (daily; may be previous calendar evening)"
Write-Host ""

$nfoTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $nfoDays -At $nfoHm
$cdsTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $cdsDays -At $cdsHm
$mcxTrigger = New-ScheduledTaskTrigger -Daily -At $mcxHm
$trayTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

Register-CmdTask -TaskName "OhlcvEodNfo" -CmdFile "eod-nfo.cmd" -Trigger $nfoTrigger `
    -Description "NFO daily+fetch after $nfoIst IST ($nfoHm local; session_end 15:40 + 30m). Logs under data/ohlcv/_eod/."
Register-CmdTask -TaskName "OhlcvEodCds" -CmdFile "eod-cds.cmd" -Trigger $cdsTrigger `
    -Description "CDS daily+fetch after $cdsIst IST ($cdsHm local; session_end 17:00 + 30m)."
Register-CmdTask -TaskName "OhlcvEodMcx" -CmdFile "eod-mcx.cmd" -Trigger $mcxTrigger `
    -Description "MCX daily+fetch after $mcxIst IST ($mcxHm local; prior trade_date). Requires catalog synced while contracts were live."
Register-CmdTask -TaskName "KiteSessionTray" -CmdFile "sessiontray.cmd" -Trigger $trayTrigger `
    -Description "Tray icon: green=valid Kite token, red=expired/missing. Menu Login runs kitetools login."

Write-Host ""
Write-Host "Done. Morning login is still manual: uv run kitetools login"
Write-Host "Re-run this script after OS DST changes so local times stay correct."
Write-Host "Unregister: powershell -ExecutionPolicy Bypass -File scripts\unregister-scheduled-tasks.ps1"
