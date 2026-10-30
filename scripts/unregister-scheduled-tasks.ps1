# Remove Task Scheduler jobs created by register-scheduled-tasks.ps1

$ErrorActionPreference = "Continue"
$names = @(
    "OhlcvEodNfo",
    "OhlcvEodCds",
    "OhlcvEodMcx",
    "KiteSessionTray"
)
foreach ($name in $names) {
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "Removed $name"
    }
    else {
        Write-Host "Skip (not found): $name"
    }
}
