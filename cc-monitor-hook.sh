#!/usr/bin/env bash
#
# cc-monitor-hook — Claude Code statusLine hook, part of claude-code-monitor
#
# Does two jobs on every status update:
#   1. Writes a session snapshot JSON to ~/.claude/session-monitor/snapshots/
#      so the external TUI monitor can pick up accurate cost + context window
#      token totals that aren't otherwise exposed.
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
#                            "total_output_tokens":800}}' \
#     | ~/.claude/cc-monitor-hook.sh

set -u  # intentionally NOT -e: a broken snapshot must not kill the statusline

SNAPSHOT_DIR="${HOME}/.claude/session-monitor/snapshots"
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
# The payload shape varies a bit across Claude Code versions; we use // 0
# or // "" so missing keys never break the jq pipeline.
session_id=$(printf '%s' "$INPUT" | jq -r '.session_id // ""')
cwd=$(printf '%s' "$INPUT" | jq -r '.workspace.current_dir // .cwd // ""')
transcript=$(printf '%s' "$INPUT" | jq -r '.transcript_path // ""')
model=$(printf '%s' "$INPUT" | jq -r '.model.display_name // .model.id // "?"')

cost_usd=$(printf '%s' "$INPUT" | jq -r '.cost.total_cost_usd // 0')
duration_ms=$(printf '%s' "$INPUT" | jq -r '.cost.total_duration_ms // 0')

ctx_pct=$(printf '%s' "$INPUT" | jq -r '.context_window.used_percentage // 0')
ctx_in=$(printf '%s' "$INPUT" | jq -r '.context_window.total_input_tokens // 0')
ctx_out=$(printf '%s' "$INPUT" | jq -r '.context_window.total_output_tokens // 0')

rl5_pct=$(printf '%s' "$INPUT" | jq -r '.rate_limits.five_hour.used_percentage // 0')
rl5_reset=$(printf '%s' "$INPUT" | jq -r '.rate_limits.five_hour.resets_at // 0')

now_ts=$(date +%s)

# ---------- Write snapshot (atomic via rename) ----------
# We only write if we have a session_id — otherwise we'd overwrite _unknown
# snapshots from different sessions.
if [ -n "$session_id" ]; then
  snapshot_path="${SNAPSHOT_DIR}/${session_id}.json"
  tmp_path="${SNAPSHOT_DIR}/.${session_id}.tmp.$$"

  # Build snapshot with jq to guarantee valid JSON (no bash string-escaping bugs).
  if printf '%s' "$INPUT" | jq --arg ts "$now_ts" '{
        snapshot_ts: ($ts | tonumber),
        session_id: (.session_id // ""),
        cwd: (.workspace.current_dir // .cwd // ""),
        transcript_path: (.transcript_path // ""),
        model: (.model.display_name // .model.id // ""),
        cost: {
          total_cost_usd: (.cost.total_cost_usd // 0),
          total_duration_ms: (.cost.total_duration_ms // 0),
          total_api_duration_ms: (.cost.total_api_duration_ms // 0),
          total_lines_added: (.cost.total_lines_added // 0),
          total_lines_removed: (.cost.total_lines_removed // 0)
        },
        context_window: {
          used_percentage: (.context_window.used_percentage // 0),
          total_input_tokens: (.context_window.total_input_tokens // 0),
          total_output_tokens: (.context_window.total_output_tokens // 0),
          cache_read_input_tokens: (.context_window.cache_read_input_tokens // 0),
          cache_creation_input_tokens: (.context_window.cache_creation_input_tokens // 0)
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
cost_fmt=$(awk -v c="$cost_usd" 'BEGIN{
  if (c < 0.01) printf "$%.4f", c;
  else if (c < 1) printf "$%.3f", c;
  else printf "$%.2f", c
}')

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
  "$C_GREEN" "$cost_fmt" "$C_RESET" \
  "$rl5_str"

exit 0
