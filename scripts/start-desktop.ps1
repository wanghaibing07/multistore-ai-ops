$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$appUrl = 'http://127.0.0.1:8018/'

function Test-AppReady {
    try {
        $response = Invoke-WebRequest -Uri $appUrl -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch {
        return $false
    }
}

if (-not (Test-AppReady)) {
    $runScript = Join-Path $projectRoot 'run-local.ps1'
    Start-Process -FilePath 'pwsh.exe' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit', '-File', $runScript) `
        -WorkingDirectory $projectRoot `
        -WindowStyle Normal

    $deadline = (Get-Date).AddSeconds(45)
    do {
        Start-Sleep -Seconds 1
    } while (-not (Test-AppReady) -and (Get-Date) -lt $deadline)
}

if (Test-AppReady) {
    Start-Process $appUrl
    exit 0
}

Add-Type -AssemblyName PresentationFramework
[System.Windows.MessageBox]::Show(
    '工作台启动失败，请查看弹出的服务窗口。',
    '店铺账本',
    [System.Windows.MessageBoxButton]::OK,
    [System.Windows.MessageBoxImage]::Error
) | Out-Null
exit 1
