#Requires -Version 5.1
<#
.SYNOPSIS
    cc-monitor-hook — Claude Code statusLine hook (PowerShell / native Windows)

.DESCRIPTION
    Part of claude-code-monitor. Does two jobs on every status update:
      1. Writes a session snapshot JSON to
         $HOME\.claude\session-monitor\snapshots\
         so the external TUI monitor can pick up accurate cost + context-window
         token totals.
      2. Prints a compact one-line status for Claude Code's status bar.

    Performance notes (from Claude Code docs):
      - Scripts run on every message turn, throttled to 300ms.
      - Slow scripts block the status line update; in-flight runs are killed
        by newer turns. So we keep this short, avoid network calls, and
        write the snapshot atomically (temp file + Move-Item rename).

    Install:
      python cc-session-monitor.py --install-hook
      (The installer detects Windows and registers this .ps1 automatically.)

    Or manually — add to %USERPROFILE%\.claude\settings.json:
      "statusLine": {
        "type": "command",
        "command": "powershell -NoProfile -NonInteractive -File %USERPROFILE%\.claude\cc-monitor-hook.ps1",
        "padding": 0
      }

    Test:
      '{"session_id":"test","model":{"display_name":"Opus"},
        "workspace":{"current_dir":"C:\\Users\\you\\myproject"},
        "cost":{"total_cost_usd":0.42},
        "context_window":{"used_percentage":15,
                          "total_input_tokens":1200,
                          "total_output_tokens":800}}' |
        powershell -NoProfile -NonInteractive -File .\cc-monitor-hook.ps1
#>

# Do NOT use Set-StrictMode -Version Latest here: a broken snapshot must never
# blank the status bar.  We use $ErrorActionPreference = 'SilentlyContinue'
# for the same reason.
$ErrorActionPreference = 'SilentlyContinue'

$SnapshotDir = Join-Path $HOME ".claude\session-monitor\snapshots"
$null = New-Item -ItemType Directory -Force -Path $SnapshotDir

# Slurp stdin once.
$Input_data = $input | Out-String

if ([string]::IsNullOrWhiteSpace($Input_data)) {
    Write-Host "cc-monitor: no input"
    exit 0
}

# Parse JSON.  If it fails, print a minimal hint and exit without error so
# the status bar always shows something.
try {
    $payload = $Input_data | ConvertFrom-Json
} catch {
    Write-Host "cc-monitor: invalid JSON payload"
    exit 0
}

# ---------- Helper: null-safe property access ----------
function Get-SafeValue {
    param(
        [Parameter(ValueFromPipeline=$true)] $Obj,
        [string[]] $Path,
        $Default = $null
    )
    $cur = $Obj
    foreach ($key in $Path) {
        if ($null -eq $cur) { return $Default }
        $cur = $cur.PSObject.Properties[$key]?.Value
    }
    if ($null -eq $cur) { return $Default }
    return $cur
}

# ---------- Extract fields with safe defaults ----------
$session_id  = Get-SafeValue $payload @("session_id") -Default ""
$cwd         = if ((Get-SafeValue $payload @("workspace","current_dir")) -ne $null) {
                   Get-SafeValue $payload @("workspace","current_dir") -Default ""
               } else {
                   Get-SafeValue $payload @("cwd") -Default ""
               }
$model       = if ((Get-SafeValue $payload @("model","display_name")) -ne $null) {
                   Get-SafeValue $payload @("model","display_name") -Default "?"
               } else {
                   Get-SafeValue $payload @("model","id") -Default "?"
               }

$cost_usd    = Get-SafeValue $payload @("cost","total_cost_usd")    -Default 0
$duration_ms = Get-SafeValue $payload @("cost","total_duration_ms") -Default 0

$ctx_pct     = Get-SafeValue $payload @("context_window","used_percentage")        -Default 0
$ctx_in      = Get-SafeValue $payload @("context_window","total_input_tokens")     -Default 0
$ctx_out     = Get-SafeValue $payload @("context_window","total_output_tokens")    -Default 0
$cache_read  = Get-SafeValue $payload @("context_window","cache_read_input_tokens")      -Default 0
$cache_create = Get-SafeValue $payload @("context_window","cache_creation_input_tokens") -Default 0

$rl5_pct     = Get-SafeValue $payload @("rate_limits","five_hour","used_percentage") -Default 0
$rl5_reset   = Get-SafeValue $payload @("rate_limits","five_hour","resets_at")       -Default 0

$now_ts = [int][double]::Parse(
    [System.DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
)

# ---------- Write snapshot (atomic via rename) ----------
if (![string]::IsNullOrWhiteSpace($session_id)) {
    $snapshot_path = Join-Path $SnapshotDir "$session_id.json"
    $tmp_path      = Join-Path $SnapshotDir ".$session_id.tmp.$$"

    $snapshot = [ordered]@{
        snapshot_ts    = $now_ts
        session_id     = [string]$session_id
        cwd            = [string]$cwd
        transcript_path = [string](Get-SafeValue $payload @("transcript_path") -Default "")
        model          = [string]$model
        cost           = [ordered]@{
            total_cost_usd         = [double]$cost_usd
            total_duration_ms      = [double]$duration_ms
            total_api_duration_ms  = [double](Get-SafeValue $payload @("cost","total_api_duration_ms") -Default 0)
            total_lines_added      = [int](Get-SafeValue $payload @("cost","total_lines_added") -Default 0)
            total_lines_removed    = [int](Get-SafeValue $payload @("cost","total_lines_removed") -Default 0)
        }
        context_window = [ordered]@{
            used_percentage              = [double]$ctx_pct
            total_input_tokens           = [int]$ctx_in
            total_output_tokens          = [int]$ctx_out
            cache_read_input_tokens      = [int]$cache_read
            cache_creation_input_tokens  = [int]$cache_create
        }
        rate_limits = if ($null -ne $payload.rate_limits) { $payload.rate_limits } else { [ordered]@{} }
        version     = [string](Get-SafeValue $payload @("version") -Default "")
    }

    try {
        $snapshot | ConvertTo-Json -Depth 10 | Set-Content -Path $tmp_path -Encoding UTF8
        Move-Item -Force -Path $tmp_path -Destination $snapshot_path
    } catch {
        $null = Remove-Item -Force -Path $tmp_path -ErrorAction SilentlyContinue
    }
}

# ---------- Print the statusline ----------
# ANSI color helpers (VT100; supported on Windows 10 1511+ / Windows Terminal).
$ESC    = [char]27
$DIM    = "${ESC}[2m"
$RESET  = "${ESC}[0m"
$GREEN  = "${ESC}[32m"
$YELLOW = "${ESC}[33m"
$RED    = "${ESC}[91m"
$CYAN   = "${ESC}[36m"

# Folder name: last path component.
$folder = if (![string]::IsNullOrWhiteSpace($cwd)) {
    Split-Path -Leaf $cwd
} else { "?" }
if ([string]::IsNullOrWhiteSpace($folder)) { $folder = "?" }

# Context bar color.
$ctx_int = [int][Math]::Floor([double]$ctx_pct)
$ctx_color = if ($ctx_int -lt 50) { $GREEN } elseif ($ctx_int -lt 80) { $YELLOW } else { $RED }

# Format cost.
$cost_fmt = if ([double]$cost_usd -lt 0.01) {
    '$' + ([double]$cost_usd).ToString("0.0000")
} elseif ([double]$cost_usd -lt 1) {
    '$' + ([double]$cost_usd).ToString("0.000")
} else {
    '$' + ([double]$cost_usd).ToString("0.00")
}

# 5-hour reset countdown.
$rl5_str = ""
if ($rl5_reset -ne 0 -and $rl5_reset -ne "null" -and $null -ne $rl5_reset) {
    $remaining = [int]$rl5_reset - $now_ts
    if ($remaining -gt 0) {
        $h = [int][Math]::Floor($remaining / 3600)
        $m = [int][Math]::Floor(($remaining % 3600) / 60)
        $rl5_pct_int = [int][Math]::Floor([double]$rl5_pct)
        $rl5_str = " ${DIM}|${RESET} 5h ${rl5_pct_int}% (${h}h${m}m)"
    }
}

# Emit the single-line statusline.  Claude Code uses only the first line.
$line = "${CYAN}${folder}${RESET} ${DIM}|${RESET} ${DIM}${model}${RESET} ${DIM}|${RESET} ctx ${ctx_color}${ctx_int}%${RESET} ${DIM}|${RESET} ${GREEN}${cost_fmt}${RESET}${rl5_str}"
Write-Host $line

exit 0
