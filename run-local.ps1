$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
try {
    $existingListener = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() | Where-Object { $_.Port -eq 8018 }
} catch {
    Write-Error '无法检查本地端口，未执行数据库迁移。'
    exit 1
}
if ($existingListener) {
    Write-Error '本地 8018 端口已被占用。请先关闭原服务，再启动工作台；未执行数据库迁移。'
    exit 1
}
uv sync --locked
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run alembic upgrade head
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run uvicorn app.main:app --host 127.0.0.1 --port 8018 --no-access-log
