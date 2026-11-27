#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet("status", "health", "logs", "backup", "deploy", "rollback")]
    [string]$Action = "status",

    [string]$HostAlias = "plan2-prod",

    [string]$RemoteDir = "/opt/plan2_redeem_service",

    [string]$ServiceName = "plan2-redeem.service",

    [ValidateRange(10, 500)]
    [int]$Lines = 80,

    [switch]$Force,

    [string]$BackupName = "",

    [ValidateRange(1, 50)]
    [int]$KeepBackups = 10,

    [ValidateRange(0, 120)]
    [int]$DrainWaitSeconds = 20
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($HostAlias -notmatch "^[A-Za-z0-9._-]+$") {
    throw "Invalid HostAlias: $HostAlias"
}
$RemoteDir = $RemoteDir.TrimEnd("/")
if ($RemoteDir -notmatch "^/[A-Za-z0-9._/-]+$" -or $RemoteDir -match "\.\.") {
    throw "Invalid RemoteDir: $RemoteDir"
}
if ($ServiceName -notmatch "^[A-Za-z0-9._@-]+$") {
    throw "Invalid ServiceName: $ServiceName"
}
if ($BackupName.Length -gt 0) {
    if ($BackupName -notmatch "^[A-Za-z0-9._-]+$" -or $BackupName -eq "." -or $BackupName -eq ".." -or $BackupName -match "\.\.") {
        throw "Invalid BackupName: $BackupName"
    }
}

function ConvertTo-PosixSingleQuoted {
    param(
        [AllowEmptyString()]
        [Parameter(Mandatory = $true)]
        [string]$Value
    )
    return "'" + $Value.Replace("'", "'`"'`"'") + "'"
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
        $quotedArgs = foreach ($arg in $RemoteArgs) {
            ConvertTo-PosixSingleQuoted -Value $arg
        }
        $remoteCmd += " " + ($quotedArgs -join " ")
    }
    $encoded | & ssh -o BatchMode=yes -o ConnectTimeout=10 $HostAlias $remoteCmd
    if ($LASTEXITCODE -ne 0) {
        throw "Remote command failed on $HostAlias (exit code $LASTEXITCODE)"
    }
}

$BashHelpers = @'
resolve_python_bin() {
  local remote_dir="$1"
  if [ -x "$remote_dir/.venv/bin/python" ]; then
    printf '%s\n' "$remote_dir/.venv/bin/python"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  echo "ERROR: Python interpreter not found at $remote_dir/.venv/bin/python or in PATH." >&2
  return 1
}

run_as_plan2() {
  if [ "$(id -u)" -eq 0 ] && id -u plan2 >/dev/null 2>&1; then
    if command -v runuser >/dev/null 2>&1; then
      runuser -u plan2 -- "$@"
      return $?
    elif command -v sudo >/dev/null 2>&1; then
      sudo -u plan2 -- "$@"
      return $?
    fi
  fi
  "$@"
}

ensure_db_sidecar_owner() {
  local db_path="$1"
  if [ "$(id -u)" -eq 0 ] && id -u plan2 >/dev/null 2>&1; then
    if ! command -v runuser >/dev/null 2>&1 && ! command -v sudo >/dev/null 2>&1; then
      local sidecar
      for sidecar in "${db_path}-wal" "${db_path}-shm"; do
        if [ -e "$sidecar" ]; then
          chown plan2:plan2 "$sidecar"
        fi
      done
    fi
  fi
}

query_purchasing_count() {
  local remote_dir="$1"
  local db_path="$remote_dir/data/redeem.sqlite3"
  if [ ! -f "$db_path" ]; then
    printf '0\n'
    return 0
  fi
  local python_bin
  python_bin="$(resolve_python_bin "$remote_dir")"
  local py_code
  py_code='import sqlite3, sys
from pathlib import Path

db_path = Path(sys.argv[1])
if not db_path.is_file():
    print(0)
    sys.exit(0)

conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
try:
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA query_only=ON")
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ? LIMIT 1",
        ("table", "codes"),
    ).fetchone()
    if table_exists is None:
        print(0)
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM codes WHERE status = ?",
            ("purchasing",),
        ).fetchone()
        print(int(row[0]) if row is not None else 0)
finally:
    conn.close()
'
  local count
  count="$(run_as_plan2 "$python_bin" -c "$py_code" "$db_path")"
  ensure_db_sidecar_owner "$db_path"
  if [[ ! "$count" =~ ^[0-9]+$ ]]; then
    echo "ERROR: Unexpected purchasing count output: $count" >&2
    return 1
  fi
  printf '%s\n' "$count"
}

wait_for_purchasing_drain() {
  local remote_dir="$1"
  local force_flag="$2"
  local drain_wait_seconds="$3"
  local action_label="$4"
  local count
  count="$(query_purchasing_count "$remote_dir")"
  if [ "$count" -eq 0 ]; then
    echo "In-flight purchasing check passed (0 active purchasing orders)."
    return 0
  fi
  if [ "$force_flag" = "1" ]; then
    echo "WARNING: Found ${count} in-flight purchasing order(s); proceeding with ${action_label} because -Force was specified." >&2
    return 0
  fi
  local elapsed=0
  while [ "$count" -gt 0 ] && [ "$elapsed" -lt "$drain_wait_seconds" ]; do
    local remaining=$((drain_wait_seconds - elapsed))
    local step=2
    if [ "$remaining" -lt 2 ]; then
      step="$remaining"
    fi
    echo "Waiting for ${count} in-flight purchasing order(s) to drain (${elapsed}s/${drain_wait_seconds}s)..."
    sleep "$step"
    elapsed=$((elapsed + step))
    count="$(query_purchasing_count "$remote_dir")"
  done
  if [ "$count" -gt 0 ]; then
    echo "ERROR: Blocked ${action_label} because ${count} order(s) remain in status='purchasing' after waiting ${drain_wait_seconds}s." >&2
    echo "Confirm no in-flight purchasing orders remain and retry, or pass -Force to override." >&2
    return 1
  fi
  echo "In-flight purchasing orders drained after ${elapsed}s."
}

create_backup_and_prune() {
  local remote_dir="$1"
  local keep_backups="$2"
  local backups_root="$remote_dir/backups"
  local db_path="$remote_dir/data/redeem.sqlite3"

  mkdir -p "$backups_root"
  chown plan2:plan2 "$backups_root"
  chmod 750 "$backups_root"

  local backup_ts
  backup_ts="$(date -u +%Y%m%dT%H%M%SZ)"
  while [ -e "$backups_root/$backup_ts" ]; do
    sleep 1
    backup_ts="$(date -u +%Y%m%dT%H%M%SZ)"
  done

  local backup_dir="$backups_root/$backup_ts"
  BACKUP_STAGE_DIR="$backups_root/.tmp-${backup_ts}-$$"
  mkdir -p "$BACKUP_STAGE_DIR"
  chown plan2:plan2 "$BACKUP_STAGE_DIR"
  chmod 750 "$BACKUP_STAGE_DIR"

  if [ -f "$db_path" ]; then
    local python_bin
    python_bin="$(resolve_python_bin "$remote_dir")"
    local py_backup_code
    py_backup_code='import sqlite3, sys
from pathlib import Path

src_path = Path(sys.argv[1])
dst_path = Path(sys.argv[2])

src = sqlite3.connect(src_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
try:
    src.execute("PRAGMA busy_timeout=30000")
    src.execute("PRAGMA query_only=ON")
    dst = sqlite3.connect(str(dst_path), timeout=30)
    try:
        src.backup(dst, pages=256, sleep=0.1)
        check = dst.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise RuntimeError(f"Backup integrity check failed: {check}")
    finally:
        dst.close()
finally:
    src.close()
'
    (
      umask 027
      run_as_plan2 "$python_bin" -c "$py_backup_code" "$db_path" "$BACKUP_STAGE_DIR/redeem.sqlite3"
    )
    ensure_db_sidecar_owner "$db_path"
    chown plan2:plan2 "$BACKUP_STAGE_DIR/redeem.sqlite3"
    chmod 640 "$BACKUP_STAGE_DIR/redeem.sqlite3"
  else
    echo "Notice: Database file not found at $db_path; skipping SQLite backup."
  fi

  local snapshot_items=()
  local candidate
  for candidate in backend tests API_CONTRACT.md README.md pyproject.toml config.example.yaml .env.example frontend/dist; do
    if [ -e "$remote_dir/$candidate" ]; then
      snapshot_items+=("$candidate")
    fi
  done

  if [ "${#snapshot_items[@]}" -gt 0 ]; then
    (
      umask 027
      tar -czf "$BACKUP_STAGE_DIR/app-snapshot.tar.gz" \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='*.pyo' \
        --exclude='.pytest_cache' \
        -C "$remote_dir" \
        "${snapshot_items[@]}"
    )
    chown plan2:plan2 "$BACKUP_STAGE_DIR/app-snapshot.tar.gz"
    chmod 640 "$BACKUP_STAGE_DIR/app-snapshot.tar.gz"
  else
    echo "Notice: No existing application files found in $remote_dir; skipping app-snapshot.tar.gz."
  fi

  chown plan2:plan2 "$BACKUP_STAGE_DIR"
  chmod 750 "$BACKUP_STAGE_DIR"
  mv "$BACKUP_STAGE_DIR" "$backup_dir"
  BACKUP_STAGE_DIR=""

  CREATED_BACKUP_NAME="$backup_ts"
  CREATED_BACKUP_DIR="$backup_dir"

  echo "Backup created: $backup_dir"
  local artifact
  for artifact in redeem.sqlite3 app-snapshot.tar.gz; do
    if [ -f "$backup_dir/$artifact" ]; then
      local human_size byte_size
      human_size="$(du -h "$backup_dir/$artifact" | cut -f1)"
      byte_size="$(wc -c < "$backup_dir/$artifact" | tr -d ' ')"
      echo "  ${artifact}: ${human_size} (${byte_size} bytes)"
    fi
  done
  local total_size
  total_size="$(du -sh "$backup_dir" | cut -f1)"
  echo "  total: ${total_size}"

  local valid_backups=()
  local entry
  while IFS= read -r entry; do
    if [ -n "$entry" ]; then
      valid_backups+=("$entry")
    fi
  done < <(
    for backup_path in "$backups_root"/*; do
      [ -d "$backup_path" ] || continue
      [ -L "$backup_path" ] && continue
      name="${backup_path##*/}"
      if [[ "$name" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
        printf '%s\n' "$name"
      fi
    done | LC_ALL=C sort -r
  )

  local pruned_count=0
  local idx old_name
  if [ "${#valid_backups[@]}" -gt "$keep_backups" ]; then
    for ((idx = keep_backups; idx < ${#valid_backups[@]}; idx += 1)); do
      old_name="${valid_backups[$idx]}"
      if [[ "$old_name" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
        rm -rf -- "$backups_root/$old_name"
        pruned_count=$((pruned_count + 1))
      fi
    done
  fi
  local kept_count=$(( ${#valid_backups[@]} - pruned_count ))
  echo "Backup retention: kept ${kept_count} (limit ${keep_backups}, pruned ${pruned_count})."
}

fix_app_permissions() {
  local remote_dir="$1"
  cd "$remote_dir"
  local dir
  for dir in backend tests; do
    if [ -d "$dir" ]; then
      find "$dir" -type d -name '__pycache__' -prune -exec rm -rf {} +
    fi
  done
  if [ -d frontend ]; then
    chown plan2:plan2 frontend
    chmod 755 frontend
  fi
  local target
  for target in backend tests API_CONTRACT.md README.md pyproject.toml config.example.yaml .env.example frontend/dist; do
    if [ -e "$target" ]; then
      chown -R plan2:plan2 "$target"
    fi
  done
  for dir in backend tests frontend/dist; do
    if [ -d "$dir" ]; then
      find "$dir" -type d -exec chmod 755 {} +
      find "$dir" -type f -exec chmod 644 {} +
    fi
  done
  local file
  for file in API_CONTRACT.md README.md pyproject.toml config.example.yaml .env.example; do
    if [ -f "$file" ]; then
      chmod 644 "$file"
    fi
  done
}

restart_and_verify_health() {
  local service_name="$1"
  local health_file="$2"
  if ! systemctl restart "$service_name"; then
    echo "Failed to restart ${service_name}; recent logs:" >&2
    journalctl -u "$service_name" -n 20 --no-pager >&2 || true
    return 1
  fi
  local healthy=0
  local attempt
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS http://127.0.0.1:8000/api/v1/health >"$health_file" 2>/dev/null; then
      healthy=1
      break
    fi
    sleep 1
  done
  local svc_state
  svc_state="$(systemctl is-active "$service_name" || true)"
  echo "service=${svc_state}"
  if [ "$healthy" -ne 1 ]; then
    echo "Health check failed after restart; recent logs:" >&2
    journalctl -u "$service_name" -n 20 --no-pager >&2 || true
    return 1
  fi
  cat "$health_file"
  echo
}
'@

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
    "backup" {
        Write-Host "Creating SQLite online backup and application snapshot on ${HostAlias}:${RemoteDir}..."
        $backupScript = "set -euo pipefail`n" + $BashHelpers + "`n" + @'
remote_dir="$1"
keep_backups="$2"
BACKUP_STAGE_DIR=""
CREATED_BACKUP_NAME=""
CREATED_BACKUP_DIR=""
cleanup() {
  if [ -n "$BACKUP_STAGE_DIR" ] && [ -d "$BACKUP_STAGE_DIR" ]; then
    rm -rf -- "$BACKUP_STAGE_DIR"
  fi
}
trap cleanup EXIT

create_backup_and_prune "$remote_dir" "$keep_backups"
'@
        Invoke-RemoteBash -Script $backupScript -RemoteArgs @($RemoteDir, [string]$KeepBackups)
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
        if ($includeItems.Count -eq 0) {
            throw "No deployable files found in $plan2Dir"
        }

        $archiveName = "plan2-deploy-" + [guid]::NewGuid().ToString("N") + ".tar.gz"
        $localArchive = Join-Path ([System.IO.Path]::GetTempPath()) $archiveName
        $remoteArchive = "/tmp/$archiveName"
        $forceFlag = if ($Force.IsPresent) { "1" } else { "0" }

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
            Write-Host "Checking in-flight purchasing orders, creating backup, deploying to ${RemoteDir}, and restarting ${ServiceName}..."
            $deployScript = "set -euo pipefail`n" + $BashHelpers + "`n" + @'
remote_dir="$1"
remote_archive="$2"
service_name="$3"
replace_dist="$4"
force_flag="$5"
keep_backups="$6"
drain_wait_seconds="$7"

BACKUP_STAGE_DIR=""
CREATED_BACKUP_NAME=""
CREATED_BACKUP_DIR=""
health_file="$(mktemp /tmp/plan2-health.XXXXXX)"
cleanup() {
  rm -f -- "$remote_archive" "$health_file"
  if [ -n "$BACKUP_STAGE_DIR" ] && [ -d "$BACKUP_STAGE_DIR" ]; then
    rm -rf -- "$BACKUP_STAGE_DIR"
  fi
}
trap cleanup EXIT

wait_for_purchasing_drain "$remote_dir" "$force_flag" "$drain_wait_seconds" "deploy"
create_backup_and_prune "$remote_dir" "$keep_backups"

cd "$remote_dir"
if [ "$replace_dist" = "1" ]; then
  mkdir -p frontend
  chown plan2:plan2 frontend
  rm -rf frontend/dist
fi
tar -xzf "$remote_archive" -C "$remote_dir"
fix_app_permissions "$remote_dir"

if ! restart_and_verify_health "$service_name" "$health_file"; then
  echo "Deploy failed health verification. Run quick rollback:" >&2
  echo "  .\\scripts\\plan2-cloud.ps1 -Action rollback" >&2
  if [ -n "$CREATED_BACKUP_NAME" ]; then
    echo "  (or target pre-deploy snapshot: .\\scripts\\plan2-cloud.ps1 -Action rollback -BackupName $CREATED_BACKUP_NAME)" >&2
  fi
  exit 1
fi
'@
            Invoke-RemoteBash -Script $deployScript -RemoteArgs @(
                $RemoteDir,
                $remoteArchive,
                $ServiceName,
                $replaceDistFlag,
                $forceFlag,
                [string]$KeepBackups,
                [string]$DrainWaitSeconds
            )
        }
        finally {
            if (Test-Path -LiteralPath $localArchive -PathType Leaf) {
                Remove-Item -LiteralPath $localArchive -Force
            }
        }
    }
    "rollback" {
        $forceFlag = if ($Force.IsPresent) { "1" } else { "0" }
        $targetLabel = if ($BackupName.Length -gt 0) { $BackupName } else { "latest snapshot" }
        Write-Host "Rolling back ${HostAlias}:${RemoteDir} using ${targetLabel}..."
        $rollbackScript = "set -euo pipefail`n" + $BashHelpers + "`n" + @'
remote_dir="$1"
service_name="$2"
backup_name="$3"
force_flag="$4"
drain_wait_seconds="$5"

health_file="$(mktemp /tmp/plan2-health.XXXXXX)"
cleanup() {
  rm -f -- "$health_file"
}
trap cleanup EXIT

backups_root="$remote_dir/backups"
if [ ! -d "$backups_root" ]; then
  echo "ERROR: Backups directory not found: $backups_root" >&2
  exit 1
fi

target_backup_name=""
target_backup_dir=""

if [ -n "$backup_name" ]; then
  if [[ ! "$backup_name" =~ ^[A-Za-z0-9._-]+$ ]] || [ "$backup_name" = "." ] || [ "$backup_name" = ".." ] || [[ "$backup_name" == *".."* ]]; then
    echo "ERROR: Invalid backup name: $backup_name" >&2
    exit 1
  fi
  target_backup_name="$backup_name"
  target_backup_dir="$backups_root/$backup_name"
  if [ ! -d "$target_backup_dir" ] || [ -L "$target_backup_dir" ]; then
    echo "ERROR: Backup directory not found: $target_backup_dir" >&2
    exit 1
  fi
  if [ ! -f "$target_backup_dir/app-snapshot.tar.gz" ]; then
    echo "ERROR: app-snapshot.tar.gz not found in $target_backup_dir" >&2
    exit 1
  fi
else
  while IFS= read -r candidate_name; do
    [ -n "$candidate_name" ] || continue
    if [ -z "$target_backup_dir" ] && [ -f "$backups_root/$candidate_name/app-snapshot.tar.gz" ]; then
      target_backup_name="$candidate_name"
      target_backup_dir="$backups_root/$candidate_name"
    fi
  done < <(
    for backup_path in "$backups_root"/*; do
      [ -d "$backup_path" ] || continue
      [ -L "$backup_path" ] && continue
      candidate_basename="${backup_path##*/}"
      [[ "$candidate_basename" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || continue
      printf '%s\n' "$candidate_basename"
    done | LC_ALL=C sort -r
  )
  if [ -z "$target_backup_dir" ]; then
    echo "ERROR: No backup directory containing app-snapshot.tar.gz found under $backups_root" >&2
    exit 1
  fi
fi

snapshot_archive="$target_backup_dir/app-snapshot.tar.gz"
echo "Selected backup for rollback: $target_backup_dir"
if ! tar -tzf "$snapshot_archive" >/dev/null; then
  echo "ERROR: Snapshot archive is corrupted or unreadable: $snapshot_archive" >&2
  exit 1
fi

wait_for_purchasing_drain "$remote_dir" "$force_flag" "$drain_wait_seconds" "rollback"

cd "$remote_dir"
rm -rf backend tests frontend/dist
tar -xzf "$snapshot_archive" -C "$remote_dir"
fix_app_permissions "$remote_dir"

if ! restart_and_verify_health "$service_name" "$health_file"; then
  exit 1
fi

echo "Rollback completed: application code and frontend/dist restored from $target_backup_name and $service_name restarted."
if [ -f "$target_backup_dir/redeem.sqlite3" ]; then
  echo "Production database was NOT overwritten to avoid losing orders created after the backup."
  echo "SQLite online backup is located at: $target_backup_dir/redeem.sqlite3 (restore manually if DB rollback is needed)."
else
  echo "Production database was NOT modified (no redeem.sqlite3 backup found in $target_backup_dir)."
fi
'@
        Invoke-RemoteBash -Script $rollbackScript -RemoteArgs @(
            $RemoteDir,
            $ServiceName,
            $BackupName,
            $forceFlag,
            [string]$DrainWaitSeconds
        )
    }
}
