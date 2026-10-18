param([Parameter(Mandatory=$true)][ValidateSet('worker','daemon')][string]$Component)
$ErrorActionPreference='Stop'
$platformRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $platformRoot
$Host.UI.RawUI.WindowTitle="Crypto Platform - $Component"
$mutexName='Local\CryptoPlatform_'+$Component+'_'+([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($platformRoot)).Replace('/','_').Replace('+','-'))
$platformMutex=[Threading.Mutex]::new($false,$mutexName)
$acquired=$false
try {
    try { $acquired=$platformMutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $acquired=$true }
    if (-not $acquired) { throw 'This component is already starting or running; no duplicate launched.' }
    $subcommand=if($Component -eq 'worker'){'live'}else{'market-data-daemon'}
    $existing=@(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match ('main\.py\s+'+$subcommand+'(\s|$)') })
    if ($existing.Count) { throw 'An existing process was found; no duplicate launched.' }
    if($Component -eq 'worker') {
        & 'C:\Python313\python.exe' main.py live --strategy-name Sma200RegimeGated --strategy-version v1 --symbol BNB/USDT --timeframe 4h --poll-seconds 15 --bootstrap-bars 1500 --bootstrap-replay-bars 350 --enable-real-orders
    } else {
        & 'C:\Python313\python.exe' main.py market-data-daemon --symbols BNB/USDT --timeframes 4h --polling-interval-seconds 30 --context-delay-seconds 0.2 --batch-size 1000 --retry-count 5 --retry-delay-seconds 2 --bootstrap-days 7 --recent-gap-bars 2000 --report-every-cycles 1 --max-cycles 0 --output-prefix market_data_daemon
    }
    if($LASTEXITCODE -ne 0) { Write-Host "Component ended with exit code $LASTEXITCODE. No automatic retry or rollback." -ForegroundColor Red }
} finally {
    if($acquired) { $platformMutex.ReleaseMutex() }
    $platformMutex.Dispose()
}
