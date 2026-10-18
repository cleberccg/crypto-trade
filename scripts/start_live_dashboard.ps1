param([ValidateSet('All','Backend','Frontend')][string]$Component='All')
$ErrorActionPreference='Stop'
$dashboardRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
function Start-DashboardPart([string]$Part, [int]$Port, [string]$Command) {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
    if ($listeners.Count) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listeners[0].OwningProcess)"
        if (($Part -eq 'Backend' -and $owner.CommandLine -match 'uvicorn webapi.app:app') -or
            ($Part -eq 'Frontend' -and $owner.CommandLine -match 'crypto.*vite')) {
            Write-Host "$Part already listening on localhost:$Port (PID $($owner.ProcessId)); no duplicate launched."
            return
        }
        throw "Port $Port is occupied by another service; no process was stopped."
    }
    $dashboardWindow = @"
`$Host.UI.RawUI.WindowTitle = 'Crypto Dashboard - $Part - localhost:$Port'
Set-Location '$dashboardRoot'
$Command
"@
    $dashboardEncoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($dashboardWindow))
    $dashboardProcess = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-NoExit','-EncodedCommand',$dashboardEncoded) -WorkingDirectory $dashboardRoot -WindowStyle Normal -PassThru
    Write-Host "$Part external PowerShell PID $($dashboardProcess.Id); localhost:$Port."
}
if ($Component -in @('All','Backend')) {
    Start-DashboardPart 'Backend' 8000 "`$env:API_DASHBOARD_READONLY='1'; & 'C:\Python313\python.exe' -B -m uvicorn webapi.app:app --host 127.0.0.1 --port 8000 --no-access-log"
}
if ($Component -in @('All','Frontend')) {
    Start-DashboardPart 'Frontend' 5173 "Set-Location '$dashboardRoot\frontend'; `$env:VITE_LIVE_MONITOR_ONLY='1'; & npm.cmd run dev -- --host 127.0.0.1 --port 5173 --strictPort"
}
