#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet("status", "health", "logs", "deploy")]
    [string]$Action = "status",

    [string]$HostAlias = "plan2-prod",

    [string]$RemoteDir = "/opt/plan2_redeem_service",

    [string]$ServiceName = "plan2-redeem.service",

    [ValidateRange(10, 500)]
    [int]$Lines = 80
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($HostAlias -notmatch "^[A-Za-z0-9._-]+$") {
    throw "Invalid HostAlias: $HostAlias"
}
if ($RemoteDir -notmatch "^/[A-Za-z0-9._/-]+$" -or $RemoteDir -eq "/" -or $RemoteDir -match "\.\.") {
    throw "Invalid RemoteDir: $RemoteDir"
}
if ($ServiceName -notmatch "^[A-Za-z0-9._@-]+$") {
    throw "Invalid ServiceName: $ServiceName"
}

function Invoke-RemoteBash {
    param(
        [Parameter(Mandatory = $true)][string]$Script,
        [string[]]$RemoteArgs = @()
    )
    $normalized = $Script.Replace("`r`n", "`n").Replace("`r", "`n")
    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($normalized))
    $remoteCmd = "base64 -di | bash -s --"
    if ($RemoteArgs.Count -gt 0) {
        $remoteCmd += " " + ($RemoteArgs -join " ")
    }
    $encoded | & ssh -o BatchMode=yes -o ConnectTimeout=10 $HostAlias $remoteCmd
    if ($LASTEXITCODE -ne 0) {
        throw "Remote command failed on $HostAlias (exit code $LASTEXITCODE)"
    }
}

switch ($Action) {
    "health" {
        Invoke-RemoteBash -Script @'
set -euo pipefail
service_name="$1"
systemctl is-active "$service_name"
curl -fsS http://127.0.0.1:8000/api/v1/health
echo
'@ -RemoteArgs @($ServiceName)
    }
    "status" {
        Invoke-RemoteBash -Script @'
set -euo pipefail
service_name="$1"
systemctl is-active "$service_name" caddy.service || true
echo '--- health ---'
curl -fsS http://127.0.0.1:8000/api/v1/health || true
echo
echo '--- service status ---'
systemctl status "$service_name" --no-pager -n 15 || true
'@ -RemoteArgs @($ServiceName)
    }
    "logs" {
        Invoke-RemoteBash -Script @'
set -euo pipefail
service_name="$1"
lines="$2"
journalctl -u "$service_name" -n "$lines" --no-pager
'@ -RemoteArgs @($ServiceName, [string]$Lines)
    }
    "deploy" {
        $repoRoot = Split-Path -Parent $PSScriptRoot
        $plan2Dir = Join-Path $repoRoot "plan2_redeem_service"
        if (-not (Test-Path -LiteralPath $plan2Dir -PathType Container)) {
            throw "plan2_redeem_service directory not found at $plan2Dir"
        }

        $includeItems = New-Object System.Collections.Generic.List[string]
        foreach ($candidate in @(
            "backend",
            "tests",
            "API_CONTRACT.md",
            "README.md",
            "pyproject.toml",
            "config.example.yaml",
            ".env.example"
        )) {
            if (Test-Path -LiteralPath (Join-Path $plan2Dir $candidate)) {
                $includeItems.Add($candidate)
            }
        }
        $hasFrontendDist = Test-Path -LiteralPath (Join-Path $plan2Dir "frontend/dist/index.html") -PathType Leaf
        if ($hasFrontendDist) {
            $includeItems.Add("frontend/dist")
        }

        $archiveName = "plan2-deploy-" + [guid]::NewGuid().ToString("N") + ".tar.gz"
        $localArchive = Join-Path ([System.IO.Path]::GetTempPath()) $archiveName
        $remoteArchive = "/tmp/$archiveName"

        try {
            Write-Host "Packaging plan2_redeem_service deploy archive..."
            $tarArgs = @(
                "-czf", $localArchive,
                "--exclude=__pycache__",
                "--exclude=*.pyc",
                "--exclude=*.pyo",
                "--exclude=.pytest_cache",
                "-C", $plan2Dir
            ) + $includeItems.ToArray()
            & tar @tarArgs
            if ($LASTEXITCODE -ne 0) {
                throw "tar packaging failed (exit code $LASTEXITCODE)"
            }

            Write-Host "Uploading archive to ${HostAlias}:${remoteArchive}..."
            & scp -o BatchMode=yes -o ConnectTimeout=10 $localArchive "${HostAlias}:${remoteArchive}"
            if ($LASTEXITCODE -ne 0) {
                throw "scp upload failed (exit code $LASTEXITCODE)"
            }

            $replaceDistFlag = if ($hasFrontendDist) { "1" } else { "0" }
            Write-Host "Extracting to ${RemoteDir}, fixing ownership, and restarting ${ServiceName}..."
            Invoke-RemoteBash -Script @'
set -euo pipefail
remote_dir="$1"
remote_archive="$2"
service_name="$3"
replace_dist="$4"
health_file="$(mktemp /tmp/plan2-health.XXXXXX)"
trap 'rm -f "$remote_archive" "$health_file"' EXIT
cd "$remote_dir"
if [ "$replace_dist" = "1" ]; then
  mkdir -p frontend
  chown plan2:plan2 frontend
  rm -rf frontend/dist
fi
tar -xzf "$remote_archive" -C "$remote_dir"
rm -rf backend/__pycache__ tests/__pycache__
for target in backend tests API_CONTRACT.md README.md pyproject.toml config.example.yaml .env.example frontend/dist; do
  if [ -e "$target" ]; then
    chown -R plan2:plan2 "$target"
  fi
done
find backend tests -type d -exec chmod 755 {} +
find backend tests -type f -exec chmod 644 {} +
if [ -d frontend/dist ]; then
  find frontend/dist -type d -exec chmod 755 {} +
  find frontend/dist -type f -exec chmod 644 {} +
fi
for file in API_CONTRACT.md README.md pyproject.toml config.example.yaml .env.example; do
  if [ -f "$file" ]; then
    chmod 644 "$file"
  fi
done
systemctl restart "$service_name"
healthy=0
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS http://127.0.0.1:8000/api/v1/health >"$health_file" 2>/dev/null; then
    healthy=1
    break
  fi
  sleep 1
done
echo "service=$(systemctl is-active "$service_name" || true)"
if [ "$healthy" -ne 1 ]; then
  echo "Health check failed after restart; recent logs:" >&2
  journalctl -u "$service_name" -n 20 --no-pager >&2 || true
  exit 1
fi
cat "$health_file"
echo
'@ -RemoteArgs @($RemoteDir, $remoteArchive, $ServiceName, $replaceDistFlag)
        }
        finally {
            if (Test-Path -LiteralPath $localArchive -PathType Leaf) {
                Remove-Item -LiteralPath $localArchive -Force
            }
        }
    }
}
