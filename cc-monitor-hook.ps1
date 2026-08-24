#Requires -Version 5.1
<#
.SYNOPSIS
    cc-monitor-hook — Claude Code statusLine hook (PowerShell / native Windows)

.DESCRIPTION
    Part of claude-code-monitor. Does two jobs on every status update:
      1. Writes a session snapshot JSON to
         $HOME\.claude\session-monitor\snapshots\
         so the external TUI monitor can pick up Claude Code's client-side
         cost estimate plus context-window values.
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
                          "total_output_tokens":800,
                          "context_window_size":200000,
                          "current_usage":{"input_tokens":200,
                                           "output_tokens":800,
                                           "cache_read_input_tokens":900,
                                           "cache_creation_input_tokens":100}}}' |
        powershell -NoProfile -NonInteractive -File .\cc-monitor-hook.ps1

    NOTE on the payload shape: the per-request cache token counts live under
    context_window.current_usage (null until the first API response), NOT flat
    on context_window. See the statusLine schema in the Claude Code docs.

    cost.total_cost_usd is an estimated standard-API-list-price equivalent,
    not authoritative billing. The snapshot also normalizes raw counter resets
    into an observed session total; the status bar labels the raw value "est".
#>

# Do NOT use Set-StrictMode -Version Latest here: a broken snapshot must never
# blank the status bar.  We use $ErrorActionPreference = 'SilentlyContinue'
# for the same reason.
$ErrorActionPreference = 'SilentlyContinue'

$SnapshotDir = if (![string]::IsNullOrWhiteSpace($env:CC_MONITOR_SNAPSHOT_DIR)) {
    $env:CC_MONITOR_SNAPSHOT_DIR
} else {
    Join-Path $HOME ".claude\session-monitor\snapshots"
}
$null = New-Item -ItemType Directory -Force -Path $SnapshotDir

# Slurp stdin once.
$inputJson = $input | Out-String

if ([string]::IsNullOrWhiteSpace($inputJson)) {
    Write-Host "cc-monitor: no input"
    exit 0
}

# Parse JSON.  If it fails, dump the raw payload for debugging and print a
# minimal hint so the status bar always shows something.
try {
    $payload = $inputJson | ConvertFrom-Json
} catch {
    try { Set-Content -Path "$SnapshotDir\_last-raw.json" -Value $inputJson -Encoding UTF8 } catch {}
    Write-Host "cc-monitor: invalid JSON payload"
    exit 0
}

# ---------- Helper: null-safe property access (PS 5.1-compatible) ----------
# NOTE: the ?. null-conditional operator requires PS 7.1+; we use an explicit
# property lookup so the hook runs on the default Windows PowerShell 5.1.
function Get-SafeValue {
    param(
        [Parameter(ValueFromPipeline=$true)] $Obj,
        [string[]] $Path,
        $Default = $null
    )
    $cur = $Obj
    foreach ($key in $Path) {
        if ($null -eq $cur) { return $Default }
        $prop = $cur.PSObject.Properties[$key]
        if ($null -eq $prop) { return $Default }
        $cur = $prop.Value
    }
    if ($null -eq $cur) { return $Default }
    return $cur
}

function Convert-ToNullableDouble {
    param($Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [byte] -or $Value -is [sbyte] -or
        $Value -is [int16] -or $Value -is [uint16] -or
        $Value -is [int32] -or $Value -is [uint32] -or
        $Value -is [int64] -or $Value -is [uint64] -or
        $Value -is [single] -or $Value -is [double] -or
        $Value -is [decimal]) {
        return [double]$Value
    }
    return $null
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

$cost_usd    = Convert-ToNullableDouble (Get-SafeValue $payload @("cost","total_cost_usd") -Default $null)
$duration_ms = Get-SafeValue $payload @("cost","total_duration_ms") -Default 0

# NOTE: as of Claude Code v2.1.132 total_input_tokens / total_output_tokens are
# *current context-window occupancy*, not cumulative session totals. The TUI
# treats them as a live gauge and derives cumulative Input/Output from JSONL.
$ctx_pct     = Get-SafeValue $payload @("context_window","used_percentage")        -Default 0
$ctx_in      = Get-SafeValue $payload @("context_window","total_input_tokens")     -Default 0
$ctx_out     = Get-SafeValue $payload @("context_window","total_output_tokens")    -Default 0
$ctx_size    = Get-SafeValue $payload @("context_window","context_window_size")    -Default 0
# Cache tokens live under context_window.current_usage (see the payload schema
# in the header). current_usage is null until the first API response, and the
# flat context_window.* path is kept as a fallback for older Claude Code builds.
$cache_read  = Get-SafeValue $payload @("context_window","current_usage","cache_read_input_tokens") -Default $null
if ($null -eq $cache_read)  { $cache_read  = Get-SafeValue $payload @("context_window","cache_read_input_tokens") -Default 0 }
$cache_create = Get-SafeValue $payload @("context_window","current_usage","cache_creation_input_tokens") -Default $null
if ($null -eq $cache_create) { $cache_create = Get-SafeValue $payload @("context_window","cache_creation_input_tokens") -Default 0 }

$rl5_pct     = Get-SafeValue $payload @("rate_limits","five_hour","used_percentage") -Default 0
$rl5_reset   = Get-SafeValue $payload @("rate_limits","five_hour","resets_at")       -Default 0
$rl7_pct     = Get-SafeValue $payload @("rate_limits","seven_day","used_percentage") -Default 0
$rl7_reset   = Get-SafeValue $payload @("rate_limits","seven_day","resets_at")       -Default 0

$now_ts = [int][System.DateTimeOffset]::UtcNow.ToUnixTimeSeconds()

# ---------- Write snapshot (atomic via rename) ----------
if (![string]::IsNullOrWhiteSpace($session_id)) {
    $snapshot_path = Join-Path $SnapshotDir "$session_id.json"
    $tmp_path      = Join-Path $SnapshotDir ".$session_id.tmp.$([guid]::NewGuid().ToString('N'))"

    $previousCost = $null
    if (Test-Path -LiteralPath $snapshot_path) {
        try {
            $previousSnapshot = Get-Content -Raw -LiteralPath $snapshot_path | ConvertFrom-Json
            $previousCost = Get-SafeValue $previousSnapshot @("cost") -Default $null
        } catch {
            $previousCost = $null
        }
    }

    $previousRaw = Convert-ToNullableDouble (Get-SafeValue $previousCost @("last_valid_raw_cost_usd") -Default $null)
    if ($null -eq $previousRaw) {
        $previousRaw = Convert-ToNullableDouble (Get-SafeValue $previousCost @("total_cost_usd") -Default $null)
    }
    $previousObserved = Convert-ToNullableDouble (Get-SafeValue $previousCost @("last_valid_observed_total_cost_usd") -Default $null)
    if ($null -eq $previousObserved) {
        $previousObserved = Convert-ToNullableDouble (Get-SafeValue $previousCost @("observed_total_cost_usd") -Default $null)
    }
    if ($null -eq $previousObserved) { $previousObserved = $previousRaw }

    $counterResetsValue = Convert-ToNullableDouble (Get-SafeValue $previousCost @("counter_resets") -Default 0)
    $counterResets = if ($null -eq $counterResetsValue) { 0 } else { [int]$counterResetsValue }

    if ($null -eq $cost_usd) {
        $observedCost = $null
        $lastValidRaw = $previousRaw
        $lastValidObserved = $previousObserved
    } elseif ($null -eq $previousRaw -or $null -eq $previousObserved) {
        $observedCost = $cost_usd
        $lastValidRaw = $cost_usd
        $lastValidObserved = $cost_usd
    } elseif ($cost_usd -lt $previousRaw) {
        $observedCost = $previousObserved + $cost_usd
        $lastValidRaw = $cost_usd
        $lastValidObserved = $observedCost
        $counterResets += 1
    } else {
        $observedCost = $previousObserved + ($cost_usd - $previousRaw)
        $lastValidRaw = $cost_usd
        $lastValidObserved = $observedCost
    }

    $snapshot = [ordered]@{
        snapshot_ts    = $now_ts
        session_id     = [string]$session_id
        cwd            = [string]$cwd
        transcript_path = [string](Get-SafeValue $payload @("transcript_path") -Default "")
        model          = [string]$model
        cost           = [ordered]@{
            total_cost_usd         = $cost_usd
            observed_total_cost_usd = $observedCost
            last_valid_raw_cost_usd = $lastValidRaw
            last_valid_observed_total_cost_usd = $lastValidObserved
            counter_resets         = $counterResets
            total_duration_ms      = [double]$duration_ms
            total_api_duration_ms  = [double](Get-SafeValue $payload @("cost","total_api_duration_ms") -Default 0)
            total_lines_added      = [int](Get-SafeValue $payload @("cost","total_lines_added") -Default 0)
            total_lines_removed    = [int](Get-SafeValue $payload @("cost","total_lines_removed") -Default 0)
        }
        context_window = [ordered]@{
            used_percentage              = [double]$ctx_pct
            total_input_tokens           = [int]$ctx_in
            total_output_tokens          = [int]$ctx_out
            context_window_size          = [int]$ctx_size
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

# Format cost. Missing/non-numeric input is unknown, never a synthetic zero.
$cost_fmt = if ($null -eq $cost_usd) {
    [string][char]0x2014
} elseif ([double]$cost_usd -lt 0.01) {
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

# 7-day (weekly) reset countdown — new in CC 2.1.132, Claude.ai Pro/Max only.
$rl7_str = ""
if ($rl7_reset -ne 0 -and $rl7_reset -ne "null" -and $null -ne $rl7_reset) {
    $remaining7 = [int]$rl7_reset - $now_ts
    if ($remaining7 -gt 0) {
        $d = [int][Math]::Floor($remaining7 / 86400)
        $h7 = [int][Math]::Floor(($remaining7 % 86400) / 3600)
        $rl7_pct_int = [int][Math]::Floor([double]$rl7_pct)
        $rl7_str = " ${DIM}|${RESET} 7d ${rl7_pct_int}% (${d}d${h7}h)"
    }
}

# Emit the single-line statusline.  Claude Code uses only the first line.
$line = "${CYAN}${folder}${RESET} ${DIM}|${RESET} ${DIM}${model}${RESET} ${DIM}|${RESET} ctx ${ctx_color}${ctx_int}%${RESET} ${DIM}|${RESET} ${GREEN}est ${cost_fmt}${RESET}${rl5_str}${rl7_str}"
Write-Host $line

exit 0
