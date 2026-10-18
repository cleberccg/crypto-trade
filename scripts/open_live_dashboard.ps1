$ErrorActionPreference='Stop'
$dashboardRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $dashboardRoot
$dashboardOpenMutex=[Threading.Mutex]::new($false,'Local\CryptoDashboardOpenLauncher')
$dashboardOpenAcquired=$false
try {
    try { $dashboardOpenAcquired=$dashboardOpenMutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $dashboardOpenAcquired=$true }
    if(-not $dashboardOpenAcquired) { Write-Host 'Dashboard launcher already running; wait for the browser.'; exit 0 }
    & (Join-Path $PSScriptRoot 'start_live_dashboard.ps1')
    $ready=$false
    for($attempt=0;$attempt -lt 60;$attempt++) {
        try {
            $health=Invoke-RestMethod 'http://127.0.0.1:8000/health' -TimeoutSec 2
            $front=Invoke-WebRequest 'http://127.0.0.1:5173/' -UseBasicParsing -TimeoutSec 2
            if($health.status -eq 'ok' -and $front.StatusCode -eq 200) {$ready=$true;break}
        } catch {}
        Start-Sleep -Seconds 1
    }
    if(-not $ready) { throw 'Dashboard did not become ready. Check its Backend and Frontend windows; no trading process was started.' }
    Start-Process 'http://127.0.0.1:5173/'
    Write-Host 'Dashboard opened. Log in as operator/admin to start services. No LIVE worker was started automatically.'
} finally {
    if($dashboardOpenAcquired) { $dashboardOpenMutex.ReleaseMutex() }
    $dashboardOpenMutex.Dispose()
}
