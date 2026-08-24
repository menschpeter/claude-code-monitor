#!/usr/bin/env bash
#
# cc-monitor-hook — Claude Code statusLine hook, part of claude-code-monitor
#
# Does two jobs on every status update:
#   1. Writes a session snapshot JSON to ~/.claude/session-monitor/snapshots/
#      so the external TUI monitor can pick up Claude Code's client-side cost
#      estimate plus context-window values that aren't otherwise exposed.
#   2. Prints a compact one-line status into Claude Code's status bar.
#
# Performance notes (from Claude Code docs):
#   - Scripts run on every message turn, throttled to 300ms.
#   - Slow scripts block the status line update; in-flight runs are killed
#     by newer turns. So we keep this short, avoid network calls, and
#     write the snapshot via a mv(1) rename for atomicity.
#   - jq is required. We detect missing jq and degrade gracefully.
#
# Install:
#   chmod +x ~/.claude/cc-monitor-hook.sh
#   Add to ~/.claude/settings.json:
#       "statusLine": {
#         "type": "command",
#         "command": "~/.claude/cc-monitor-hook.sh",
#         "padding": 0
#       }
#
# Test:
#   echo '{"session_id":"test","model":{"display_name":"Opus"},
#          "workspace":{"current_dir":"/tmp"},
#          "cost":{"total_cost_usd":0.42},
#          "context_window":{"used_percentage":15,
#                            "total_input_tokens":1200,
#                            "total_output_tokens":800,
#                            "context_window_size":200000,
#                            "current_usage":{"input_tokens":200,
#                                             "output_tokens":800,
#                                             "cache_read_input_tokens":900,
#                                             "cache_creation_input_tokens":100}}}' \
#     | ~/.claude/cc-monitor-hook.sh
#
# NOTE on the payload shape: the per-request cache token counts live under
# context_window.current_usage (null until the first API response), NOT flat on
# context_window. See the statusLine schema in the Claude Code docs.
# `cost.total_cost_usd` is an estimated standard-API-list-price equivalent,
# not authoritative billing. The snapshot also normalizes raw counter resets
# into an observed session total; the status bar labels the raw value `est`.

set -u  # intentionally NOT -e: a broken snapshot must not kill the statusline

SNAPSHOT_DIR="${CC_MONITOR_SNAPSHOT_DIR:-${HOME}/.claude/session-monitor/snapshots}"
mkdir -p "$SNAPSHOT_DIR" 2>/dev/null || true

# Slurp stdin once — we need it for both the snapshot and the statusline.
INPUT=$(cat)

# ---------- Graceful jq-missing fallback ----------
if ! command -v jq >/dev/null 2>&1; then
  # No jq: still write the raw payload so the TUI can parse it itself,
  # and print a minimal statusline that tells the user what's wrong.
  if [ -n "$INPUT" ]; then
    ts=$(date +%s)
    tmp="${SNAPSHOT_DIR}/.raw-${ts}-$$.json"
    printf '%s' "$INPUT" > "$tmp" 2>/dev/null
    mv "$tmp" "${SNAPSHOT_DIR}/_last-raw.json" 2>/dev/null || rm -f "$tmp"
  fi
  printf 'cc-monitor: install jq for full features'
  exit 0
fi

# ---------- Extract fields with null-safe defaults ----------
# The payload shape varies a bit across Claude Code versions; missing keys
# must never break the jq pipeline. Cost is deliberately not defaulted to 0:
# unavailable estimates stay unknown all the way to the UI.
session_id=$(printf '%s' "$INPUT" | jq -r '.session_id // ""')
cwd=$(printf '%s' "$INPUT" | jq -r '.workspace.current_dir // .cwd // ""')
transcript=$(printf '%s' "$INPUT" | jq -r '.transcript_path // ""')
model=$(printf '%s' "$INPUT" | jq -r '.model.display_name // .model.id // "?"')

cost_usd=$(printf '%s' "$INPUT" | jq -r '
  if (.cost.total_cost_usd | type) == "number"
  then (.cost.total_cost_usd | tostring)
  else "null"
  end')
duration_ms=$(printf '%s' "$INPUT" | jq -r '.cost.total_duration_ms // 0')

# NOTE: as of Claude Code v2.1.132 context_window.total_input_tokens /
# total_output_tokens are *current context-window occupancy* (from the most
# recent API response), not cumulative session totals. The TUI treats them as
# a live gauge (its "Ctx" column) and derives cumulative Input/Output from the
# JSONL transcript instead. We still snapshot them for the gauge.
ctx_pct=$(printf '%s' "$INPUT" | jq -r '.context_window.used_percentage // 0')

rl5_pct=$(printf '%s' "$INPUT" | jq -r '.rate_limits.five_hour.used_percentage // 0')
rl5_reset=$(printf '%s' "$INPUT" | jq -r '.rate_limits.five_hour.resets_at // 0')
rl7_pct=$(printf '%s' "$INPUT" | jq -r '.rate_limits.seven_day.used_percentage // 0')
rl7_reset=$(printf '%s' "$INPUT" | jq -r '.rate_limits.seven_day.resets_at // 0')

now_ts=$(date +%s)

# ---------- Write snapshot (atomic via rename) ----------
# We only write if we have a session_id — otherwise we'd overwrite _unknown
# snapshots from different sessions.
if [ -n "$session_id" ]; then
  snapshot_path="${SNAPSHOT_DIR}/${session_id}.json"
  tmp_path="${SNAPSHOT_DIR}/.${session_id}.tmp.$$"

  # The previous atomic snapshot is the normalization state. Legacy snapshots
  # only carried total_cost_usd, so that value seeds both prior counters.
  previous_cost='{}'
  if [ -f "$snapshot_path" ]; then
    previous_cost=$(jq -c '.cost // {}' "$snapshot_path" 2>/dev/null) || previous_cost='{}'
  fi

  # Build snapshot with jq to guarantee valid JSON (no bash string-escaping bugs).
  if printf '%s' "$INPUT" | jq --arg ts "$now_ts" --argjson prev "$previous_cost" '
      def numeric_or_null: if type == "number" then . else null end;
      (.cost.total_cost_usd | numeric_or_null) as $raw
      | (($prev.last_valid_raw_cost_usd | numeric_or_null)
          // ($prev.total_cost_usd | numeric_or_null)) as $prev_raw
      | (($prev.last_valid_observed_total_cost_usd | numeric_or_null)
          // ($prev.observed_total_cost_usd | numeric_or_null)
          // ($prev.total_cost_usd | numeric_or_null)) as $prev_observed
      | (($prev.counter_resets | numeric_or_null) // 0 | floor) as $prev_resets
      | (if $raw == null then
          {
            raw: null,
            observed: null,
            last_valid: $prev_raw,
            last_valid_observed: $prev_observed,
            resets: $prev_resets
          }
        elif $prev_raw == null or $prev_observed == null then
          {
            raw: $raw,
            observed: $raw,
            last_valid: $raw,
            last_valid_observed: $raw,
            resets: $prev_resets
          }
        elif $raw < $prev_raw then
          {
            raw: $raw,
            observed: ($prev_observed + $raw),
            last_valid: $raw,
            last_valid_observed: ($prev_observed + $raw),
            resets: ($prev_resets + 1)
          }
        else
          {
            raw: $raw,
            observed: ($prev_observed + $raw - $prev_raw),
            last_valid: $raw,
            last_valid_observed: ($prev_observed + $raw - $prev_raw),
            resets: $prev_resets
          }
        end) as $cost_state
      | {
        snapshot_ts: ($ts | tonumber),
        session_id: (.session_id // ""),
        cwd: (.workspace.current_dir // .cwd // ""),
        transcript_path: (.transcript_path // ""),
        model: (.model.display_name // .model.id // ""),
        cost: {
          total_cost_usd: $cost_state.raw,
          observed_total_cost_usd: $cost_state.observed,
          last_valid_raw_cost_usd: $cost_state.last_valid,
          last_valid_observed_total_cost_usd: $cost_state.last_valid_observed,
          counter_resets: $cost_state.resets,
          total_duration_ms: (.cost.total_duration_ms // 0),
          total_api_duration_ms: (.cost.total_api_duration_ms // 0),
          total_lines_added: (.cost.total_lines_added // 0),
          total_lines_removed: (.cost.total_lines_removed // 0)
        },
        context_window: {
          used_percentage: (.context_window.used_percentage // 0),
          total_input_tokens: (.context_window.total_input_tokens // 0),
          total_output_tokens: (.context_window.total_output_tokens // 0),
          context_window_size: (.context_window.context_window_size // 0),
          cache_read_input_tokens: (.context_window.current_usage.cache_read_input_tokens // .context_window.cache_read_input_tokens // 0),
          cache_creation_input_tokens: (.context_window.current_usage.cache_creation_input_tokens // .context_window.cache_creation_input_tokens // 0)
        },
        rate_limits: (.rate_limits // {}),
        version: (.version // "")
      }' > "$tmp_path" 2>/dev/null; then
    mv "$tmp_path" "$snapshot_path" 2>/dev/null || rm -f "$tmp_path"
  else
    rm -f "$tmp_path" 2>/dev/null
  fi
fi

# ---------- Print the statusline ----------
# Short folder name (last path component only).
folder=$(basename "$cwd" 2>/dev/null)
[ -z "$folder" ] && folder="?"

# ANSI colors (use $'...' so the escapes actually expand).
C_DIM=$'\033[2m'
C_RESET=$'\033[0m'
C_GREEN=$'\033[32m'
C_YELLOW=$'\033[33m'
C_RED=$'\033[91m'
C_CYAN=$'\033[36m'

# Context bar color.
ctx_int=${ctx_pct%.*}
[ -z "$ctx_int" ] && ctx_int=0
if [ "$ctx_int" -lt 50 ]; then
  ctx_color="$C_GREEN"
elif [ "$ctx_int" -lt 80 ]; then
  ctx_color="$C_YELLOW"
else
  ctx_color="$C_RED"
fi

# Format cost as $X.XX or $X.XXX for small values.
if [ "$cost_usd" = "null" ]; then
  cost_fmt="—"
else
  cost_fmt=$(awk -v c="$cost_usd" 'BEGIN{
    if (c < 0.01) printf "$%.4f", c;
    else if (c < 1) printf "$%.3f", c;
    else printf "$%.2f", c
  }')
fi

# 5h reset countdown, if available.
rl5_str=""
if [ "$rl5_reset" != "0" ] && [ "$rl5_reset" != "null" ]; then
  remaining=$(( rl5_reset - now_ts ))
  if [ "$remaining" -gt 0 ]; then
    h=$(( remaining / 3600 ))
    m=$(( (remaining % 3600) / 60 ))
    rl5_str=" ${C_DIM}│${C_RESET} 5h ${rl5_pct%.*}% (${h}h${m}m)"
  fi
fi

# 7d (weekly) reset countdown — new in CC 2.1.132, Claude.ai Pro/Max only.
rl7_str=""
if [ "$rl7_reset" != "0" ] && [ "$rl7_reset" != "null" ]; then
  remaining7=$(( rl7_reset - now_ts ))
  if [ "$remaining7" -gt 0 ]; then
    d=$(( remaining7 / 86400 ))
    h7=$(( (remaining7 % 86400) / 3600 ))
    rl7_str=" ${C_DIM}│${C_RESET} 7d ${rl7_pct%.*}% (${d}d${h7}h)"
  fi
fi

# Note: rl5_str uses a single % because printf's %b expands escapes but
# does not interpret format specifiers — the literal % passes through.

# The statusline only keeps the first line of stdout.
printf '%s%s%s %s│%s %s%s%s %s│%s ctx %s%s%%%s %s│%s %s%s%s%b\n' \
  "$C_CYAN" "$folder" "$C_RESET" \
  "$C_DIM" "$C_RESET" \
  "$C_DIM" "$model" "$C_RESET" \
  "$C_DIM" "$C_RESET" \
  "$ctx_color" "$ctx_int" "$C_RESET" \
  "$C_DIM" "$C_RESET" \
  "$C_GREEN" "est $cost_fmt" "$C_RESET" \
  "${rl5_str}${rl7_str}"

exit 0
