# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

A three-piece tool for observing Claude Code token usage and estimated API list-price cost in real time:

- `cc-monitor-hook.sh` — a `statusLine` hook that both renders Claude Code's status bar AND dumps a per-session snapshot JSON to disk on every turn.
- `cc-session-monitor.py` — a `rich`-based TUI that reads those snapshots plus the Claude Code JSONL transcripts and shows live token/cost/velocity tables per session.
- `cc_history.py` — a persistence layer the TUI imports: one JSON file per calendar day under `~/.claude/session-monitor/history/daily/`, rolled into monthly JSONL files with bounded retention. Missing days in the last 3 are reconstructed from JSONL on startup.

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

There is no build step and no lint config. Unit tests cover `cc_history.py`,
hook cost normalization (`test_hook_cost.py`), `install_hook()`
(`test_install_hook.py`), and the TUI's context, cost attribution/rendering,
and effort footer (`test_tui_context.py`):

```bash
./.venv/bin/pytest tests/ -v
```

## Architecture

**Data flow.** Claude Code invokes `cc-monitor-hook.sh` on every message turn and pipes a JSON payload (session_id, model, cost, context_window, rate_limits) into it on stdin. The hook (a) prints one colored status line to stdout for the status bar, and (b) writes `~/.claude/session-monitor/snapshots/<session_id>.json` via a `mv(1)` rename for atomicity. The TUI separately tails `~/.claude/projects/*/<session_id>.jsonl` transcript files AND reads those snapshots, merges both sources per session, and renders two tables: an "Active" window (last 15 min) and a calendar-day "Today" window (reset at local midnight, consistent with `cc_history.py`'s per-day persistence). Anthropic's 5h and 7d rate-limit resets are shown in the status bar via `rate_limits.five_hour` / `rate_limits.seven_day` (the latter is new in CC 2.1.132, Claude.ai Pro/Max only) and are independent of the TUI's window constants.

**Why the hook exists — critical invariant.** The `input_tokens` and `output_tokens` fields in Claude Code's JSONL transcripts are streaming placeholders; they undercount API usage and get duplicated across streaming chunks. The hook snapshot is the only local source of `cost.total_cost_usd`, which Claude Code computes client-side as a current-session estimate at standard Anthropic API list prices. It is not an invoice: Pro/Max included usage is not a per-session API charge, and provider billing consoles remain authoritative. Cache token fields are reliable in the JSONL and come from there. Cumulative Input/Output come from the JSONL merge (approximate but monotonic). Rows are marked `●` (snapshot present, estimate may still be unknown, live Ctx gauge available) vs `○` (JSONL-only) in the TUI.

**Cost accounting semantics.** The raw `cost.total_cost_usd` counter can restart at a lower value. Each hook reads the previous atomic snapshot and writes a reset-safe `observed_total_cost_usd`: increases add only the delta, while a lower raw value starts a new epoch and adds the new raw amount. `last_valid_raw_cost_usd`, `last_valid_observed_total_cost_usd`, and `counter_resets` preserve normalization state across missing samples; missing or non-numeric cost stays JSON `null`. The status line deliberately says `est $…` / `est —`. In the TUI, Active uses the observed session estimate; Today uses deltas attributed by `snapshot_ts` to a local calendar date. An old session without a saved daily baseline remains unknown rather than assigning its lifetime estimate to today.

**Context-window semantics — changed in CC v2.1.132.** Before v2.1.132 the hook's `context_window.total_input_tokens` / `total_output_tokens` were *cumulative session totals* and `build_table` preferred them for the Input/Output columns. As of v2.1.132 they report *current context-window occupancy* (from the most recent API response) — they rise and fall (e.g. drop after `/compact`). The TUI therefore (a) derives cumulative Input/Output/Total from JSONL only, and (b) surfaces the hook's current-window numbers in the separate **Ctx** column (`used/size`, using the new `context_window.context_window_size`). If you ever reintroduce a cumulative token field from the hook, do NOT route it back into Input/Output without confirming it is cumulative again — see `_fmt_ctx` and the Input/Output assignment in `build_table`, plus `tests/test_tui_context.py`.

**Where the payload's cache tokens live.** Per-request cache counts are nested at `context_window.current_usage.{cache_read_input_tokens,cache_creation_input_tokens}` — *not* flat on `context_window`, and `current_usage` is `null` until the first API response. Both hooks read the nested path with a fallback to the old flat one. These snapshot fields are currently informational only (the TUI's cache columns come from the JSONL, which is authoritative for cache tokens); verified against the statusLine schema bundled in Claude Code 2.1.221.

**Effort level comes from the JSONL, not the hook.** Claude Code exposes the reasoning effort twice: as `effort.level` in the statusLine payload (live setting, hook-only, present only when the model supports effort) and as a flat `effort` string on `assistant` entries in the JSONL (effort actually used for that request). The TUI reads the JSONL via `extract_effort` — no hook change, so no re-install, and `○` sessions are covered too. Two consequences are accepted by design: the value lags a `/effort` change until the next assistant turn, and "most recent session" is decided by JSONL `last_ts` rather than snapshot time. `SessionState.effort` is deliberately not a `UsageSample` field — `merge_sample` is a per-field MAX merge, meaningless for a string. `_fmt_effort_footer` returns `None` instead of falling through to an older session, because the rendered line names a specific session. If you ever route the hook's `effort.level` in as well, keep the precedence explicit and update `tests/test_tui_context.py`. Note `_fmt_effort_footer` picks "most recent" by JSONL `last_ts` alone, while `active_sessions()` (the Active table) sorts by `effective_last_ts()` = `max(last_ts, hook_ts)` — a session with a fresh hook snapshot but no assistant entry yet can top the Active table while the footer names a different session; this is accepted, not a bug, because switching the footer to `effective_last_ts()` would make the line vanish mid-turn for a session with a snapshot but no assistant entry.

**Dedup strategy.** `_merge_sample` does a per-field MAX merge keyed by `requestId` (fallback: message id, then `ts:<timestamp>`). Streaming duplicates are normal — never sum raw JSONL entries; always merge-then-sum.

**Three velocities, deliberately orthogonal.** `velocity()` (`t/s`) sums all token types equally — cache-reads count as much as output tokens even though they cost much less. `output_velocity()` (`out/s`) restricts to `output_tokens` and is the "real generation rate" signal. `cost_velocity()` (`$/h`) operates on normalized observed estimate points, never raw counter resets. Do not derive it from any token rate × a price constant; the tool has no price table and must not grow one. It is an estimated standard-list-price rate, not authoritative billing. Both `t/s` and `out/s` are rendered in a single neutral cyan — they are informational, not warnings; only `$/h` uses red/yellow/green thresholds. Both `velocity()` and `output_velocity()` delegate to the shared `_series_rate()` helper — keep the rate math in one place.

**`out/s` accuracy.** `out/s` is derived from `output_tokens` in JSONL samples — the same fields that are streaming placeholders and undercount API output. The hook cannot supply an output-token series, so there is no hook-backed alternative. This caveat is documented in README's "Known limitations" — keep it accurate if you change the source of `output_velocity_points`. For actual charges, point users at the Claude Console Usage and Cost API or their provider's billing console; the TUI dollar columns remain list-price estimates.

**Tail-only reads.** `Monitor.refresh` remembers the last `file_size` per JSONL and seeks to that offset; if the file shrank (edit / rotate) it resets in-memory state and re-reads from 0. A trailing non-newline-terminated chunk is skipped to avoid parsing a mid-write line.

**Hook performance constraints** (from the Claude Code docs, reproduced in the hook header): the script runs on every turn, is throttled to 300 ms, and a newer turn *kills* an in-flight run. So the hook must stay fast: no network calls, `set -u` but deliberately NOT `set -e` (a broken snapshot must never blank the status bar), and the snapshot is always written to a temp file then renamed. `jq` missing is handled gracefully — the raw payload is dumped to `_last-raw.json` and a minimal status line is printed.

**Project-name humanization.** Claude Code encodes a project's cwd as a directory name like `-Users-peter-code-myproj`. `_humanize_project` takes only the last `-`-split segment. If you change that scheme, update the decoder here AND the TUI's Project column will silently break.

**History logger split.** `cc_history.py` owns daily/monthly persistence plus the shared JSONL parsing helpers (`parse_ts`, `extract_usage`, `merge_sample`, `humanize_project`, and the `UsageSample` dataclass). `cc-session-monitor.py` imports them with `_`-prefixed aliases to preserve existing call sites; do not re-implement these locally. Every startup re-runs retention, fills missing-day gaps by scanning JSONL, then restores today's cost baseline before the first monitor refresh. Daily entries persist `estimated_cost_usd`, the last observed/raw totals, reset count, and last cost timestamp. Reconstructed files set estimated and legacy cumulative cost to `null`; post-hoc JSONL cannot reproduce or attribute the hook estimate. `session_cumulative_cost_usd` remains serialized only as a deprecated compatibility field and must never be summed across dates. Write cadence is every 60 s in the main loop plus an `atexit` flush; both call `run_retention` so a monitor spanning midnight rolls yesterday into monthly at the first post-midnight tick.
