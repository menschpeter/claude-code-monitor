# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

A two-piece tool for observing Claude Code token usage and cost in real time:

- `cc-monitor-hook.sh` — a `statusLine` hook that both renders Claude Code's status bar AND dumps a per-session snapshot JSON to disk on every turn.
- `cc-session-monitor.py` — a `rich`-based TUI that reads those snapshots plus the Claude Code JSONL transcripts and shows live token/cost/velocity tables per session.

## Commands

```bash
# Dependencies
.venv/bin/pip install rich    # jq must also be on PATH (brew install jq)

# One-shot install: copies hook to ~/.claude/cc-monitor-hook.sh and
# patches ~/.claude/settings.json with the statusLine entry.
.venv/bin/python cc-session-monitor.py --install-hook

# Run the live monitor (second terminal, after restarting Claude Code).
.venv/bin/python cc-session-monitor.py
.venv/bin/python cc-session-monitor.py --refresh 1.0 --velocity-window 30

# Smoke-test the hook script directly (see the docstring at the top of
# cc-monitor-hook.sh for a ready-to-pipe JSON payload).
echo '{"session_id":"test", ...}' | ./cc-monitor-hook.sh
```

There is no build step, no lint config, and no test suite. The project is two standalone scripts.

## Architecture

**Data flow.** Claude Code invokes `cc-monitor-hook.sh` on every message turn and pipes a JSON payload (session_id, model, cost, context_window, rate_limits) into it on stdin. The hook (a) prints one colored status line to stdout for the status bar, and (b) writes `~/.claude/session-monitor/snapshots/<session_id>.json` via a `mv(1)` rename for atomicity. The TUI separately tails `~/.claude/projects/*/<session_id>.jsonl` transcript files AND reads those snapshots, merges both sources per session, and renders two tables: an "Active" window (last 5 min) and the rolling 5h billing window.

**Why the hook exists — critical invariant.** The `input_tokens` and `output_tokens` fields in Claude Code's JSONL transcripts are streaming placeholders; they undercount real billed usage and get duplicated across streaming chunks. The hook's snapshot is the *only* source of accurate `total_cost_usd` and `context_window` totals. In `build_table` (cc-session-monitor.py) hook-backed values always take precedence over JSONL values for Input/Output; rows are marked `●` (hook) vs `○` (JSONL-only) in the TUI to make this visible. Cache token fields *are* accurate in the JSONL and come from there.

**Dedup strategy.** `_merge_sample` does a per-field MAX merge keyed by `requestId` (fallback: message id, then `ts:<timestamp>`). Streaming duplicates are normal — never sum raw JSONL entries; always merge-then-sum.

**`t/s` vs `$/h` are orthogonal.** `velocity()` sums all token types equally — cache-reads count as much as output tokens even though they cost ~100× less. That means a red `t/s` often coincides with a green `$/h` (cache is working). Don't "simplify" `$/h` by deriving it from `t/s` × a price constant; the tool has no price table and must not grow one (see the README's "Why this exists" section). `cost_velocity()` is the only cost-rate computation and it operates on the hook's `total_cost_usd` directly.

**Tail-only reads.** `Monitor.refresh` remembers the last `file_size` per JSONL and seeks to that offset; if the file shrank (edit / rotate) it resets in-memory state and re-reads from 0. A trailing non-newline-terminated chunk is skipped to avoid parsing a mid-write line.

**Hook performance constraints** (from the Claude Code docs, reproduced in the hook header): the script runs on every turn, is throttled to 300 ms, and a newer turn *kills* an in-flight run. So the hook must stay fast: no network calls, `set -u` but deliberately NOT `set -e` (a broken snapshot must never blank the status bar), and the snapshot is always written to a temp file then renamed. `jq` missing is handled gracefully — the raw payload is dumped to `_last-raw.json` and a minimal status line is printed.

**Project-name humanization.** Claude Code encodes a project's cwd as a directory name like `-Users-peter-code-myproj`. `_humanize_project` takes only the last `-`-split segment. If you change that scheme, update the decoder here AND the TUI's Project column will silently break.
