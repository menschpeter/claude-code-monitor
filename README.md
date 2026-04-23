# cc-session-monitor

A live terminal UI for tracking [Claude Code](https://claude.com/claude-code) token usage, cost, and velocity across all your active sessions — plus a `statusLine` hook that upgrades Claude Code's own status bar with accurate cost and context-window info.

Two components work together:

- **`cc-monitor-hook.sh`** — a `statusLine` hook that runs on every message turn. It renders a compact colored status line *and* drops a per-session snapshot JSON to disk.
- **`cc-session-monitor.py`** — a [`rich`](https://github.com/Textualize/rich)-based TUI that reads those snapshots plus Claude Code's JSONL transcripts and shows live per-session tables.

## Why this exists

Claude Code writes streaming transcripts to `~/.claude/projects/<project>/<session>.jsonl`. The `input_tokens` and `output_tokens` fields in those logs are **streaming placeholders** — they undercount real billed usage and get duplicated across chunks. Cache token fields are accurate, but cost and context-window totals are not present in the JSONL at all.

The `statusLine` hook API, on the other hand, receives accurate `total_cost_usd` and `context_window` values on every turn. This project bridges the two:

1. The hook persists each turn's snapshot to `~/.claude/session-monitor/snapshots/<session_id>.json`.
2. The TUI merges those snapshots with JSONL-derived cache tokens and renders the whole picture.

Rows in the TUI are marked `●` (hook-backed, accurate cost + tokens) or `○` (JSONL-only, approximate) so you always know which numbers to trust.

## Features

- **Two views side by side**: "Active" (sessions with activity in the last 5 minutes) and "Billing" (everything in the rolling 5-hour window).
- **Per-session breakdown**: session id, project, last-activity age, input / output / cache-read / total tokens, cost, and two velocities.
- **Velocity columns**: tokens/second and USD/hour, both over a configurable rolling window.
- **Upgraded status bar**: folder · model · context % (green/yellow/red) · cost · 5h reset countdown, directly in Claude Code.
- **Graceful degradation**: if `jq` is missing the hook still writes the raw payload and prints a minimal hint instead of silently breaking.
- **Safe by default**: atomic snapshot writes via `mv(1)`, tail-only JSONL reads, and the hook deliberately does not `set -e` so a bad payload can never blank your status bar.

## Requirements

- Python 3.10+
- [`rich`](https://pypi.org/project/rich/) — `pip install rich`
- `jq` on `PATH` — `brew install jq` / `apt install jq`
- Claude Code installed and having run at least once (so `~/.claude/` exists)

## Installation

```bash
# 1. Install Python deps
pip install rich

# 2. Install the statusLine hook into ~/.claude/ and patch settings.json
python cc-session-monitor.py --install-hook

# 3. Restart any running Claude Code sessions so the hook picks up

# 4. In a second terminal, launch the monitor
python cc-session-monitor.py
```

A convenience wrapper is included:

```bash
./run-monitor.sh                 # starts the monitor via the local .venv
./run-monitor.sh --install-hook  # install the hook
```

### What `--install-hook` does

- Copies `cc-monitor-hook.sh` to `~/.claude/cc-monitor-hook.sh` and `chmod +x`es it.
- Adds (or, after confirmation, replaces) the `statusLine` entry in `~/.claude/settings.json`:
  ```json
  "statusLine": {
    "type": "command",
    "command": "~/.claude/cc-monitor-hook.sh",
    "padding": 0
  }
  ```
- Never touches anything outside `~/.claude/`.

## Usage

```bash
python cc-session-monitor.py [OPTIONS]
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--refresh <sec>` | `1.0` | TUI redraw interval in seconds (float). Smaller = smoother, more CPU. |
| `--velocity-window <sec>` | `30` | Rolling window for the `t/s` and `$/h` velocity columns. Larger = smoother, smaller = more reactive. |
| `--projects-dir <path>` | `~/.claude/projects` | Where to read JSONL transcripts from. Change only if Claude Code stores data elsewhere. |
| `--snapshot-dir <path>` | `~/.claude/session-monitor/snapshots` | Where the hook writes snapshots. Must match the hook's target. |
| `--install-hook` | — | One-shot: install the hook into `~/.claude/` and exit. No monitor run. |
| `-h`, `--help` | — | Show help and exit. |

### Example

```bash
# Faster redraw + smaller velocity window for near-realtime feedback
python cc-session-monitor.py --refresh 0.5 --velocity-window 10
```

## How it works

```
┌─────────────────┐   every turn   ┌──────────────────────┐
│   Claude Code   │ ─────────────▶ │  cc-monitor-hook.sh  │
└─────────────────┘   (stdin JSON)  └──────────┬───────────┘
       │                                       │
       │ writes                                │ writes snapshot
       ▼                                       ▼
 ~/.claude/projects/*/*.jsonl      ~/.claude/session-monitor/snapshots/
       │                                       │
       └───────────┐             ┌─────────────┘
                   ▼             ▼
              ┌─────────────────────────┐
              │  cc-session-monitor.py  │
              │   (rich TUI, tailing)   │
              └─────────────────────────┘
```

- **Dedup**: usage samples are merged per `requestId` using a per-field `MAX` strategy so streaming duplicates don't double-count.
- **Tail-only reads**: the TUI remembers each JSONL's last-seen size and `seek()`s there; if a file shrinks (rotate / edit) it resets and re-reads from 0.
- **Atomic snapshots**: the hook writes to a temp path then `mv`s it — a partially written file can never confuse the reader.
- **Performance**: the hook stays well under Claude Code's 300 ms turn throttle (no network, `jq` only, single stdin read).

## Known limitations

- **JSONL input/output tokens undercount.** This is a property of Claude Code's transcripts (see [gille.ai's analysis](https://gille.ai/)), not of this tool. Until the hook has fired at least once for a session, the TUI falls back to JSONL values and marks the row `○` to flag that they're approximate.
- **Cache tokens come from JSONL only** — the `statusLine` payload doesn't expose them directly. They are reliable there.
- **Sessions outside `~/.claude/projects/`** (rare) are discovered only when the hook fires, since discovery normally walks that directory.

## Files

```
.
├── cc-session-monitor.py   # the TUI
├── cc-monitor-hook.sh      # the statusLine hook
├── run-monitor.sh          # convenience wrapper (uses ./.venv)
├── install_cc-monitor.md   # short install note (DE)
└── CLAUDE.md               # guidance for Claude Code working in this repo
```

## Development

There is no build step, no lint config, and no test suite. The repo is two standalone scripts.

To smoke-test the hook in isolation:

```bash
echo '{"session_id":"test","model":{"display_name":"Opus"},
       "workspace":{"current_dir":"/tmp"},
       "cost":{"total_cost_usd":0.42},
       "context_window":{"used_percentage":15,
                         "total_input_tokens":1200,
                         "total_output_tokens":800}}' \
  | ./cc-monitor-hook.sh
```

A snapshot should appear under `~/.claude/session-monitor/snapshots/test.json` and a colored status line on stdout.
