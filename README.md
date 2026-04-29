# claude-code-monitor

A live terminal UI for tracking [Claude Code](https://claude.com/claude-code) token usage, cost, and velocity across all your active sessions — plus a `statusLine` hook that upgrades Claude Code's own status bar with accurate cost and context-window info.

The scripts inside use a short `cc-` prefix (short for "Claude Code") for internal consistency — the repo name spells it out for discoverability.

Three components work together:

- **`cc-monitor-hook.sh`** — a `statusLine` hook that runs on every message turn. It renders a compact colored status line *and* drops a per-session snapshot JSON to disk.
- **`cc-session-monitor.py`** — a [`rich`](https://github.com/Textualize/rich)-based TUI that reads those snapshots plus Claude Code's JSONL transcripts and shows live per-session tables.
- **`cc_history.py`** — persistent per-day history: one JSON file per calendar day, rolled into monthly JSONL files with bounded retention.

## Why this exists

Claude Code writes streaming transcripts to `~/.claude/projects/<project>/<session>.jsonl`. The `input_tokens` and `output_tokens` fields in those logs are **streaming placeholders** — they undercount real billed usage and get duplicated across chunks. Cache token fields are accurate, but cost and context-window totals are not present in the JSONL at all.

The `statusLine` hook API, on the other hand, receives accurate `total_cost_usd` and `context_window` values on every turn. This project bridges the two:

1. The hook persists each turn's snapshot to `~/.claude/session-monitor/snapshots/<session_id>.json`.
2. The TUI merges those snapshots with JSONL-derived cache tokens and renders the whole picture.

Rows in the TUI are marked `●` (hook-backed, accurate cost + tokens) or `○` (JSONL-only, approximate) so you always know which numbers to trust.

## Features

- **Two views side by side**: "Active" (sessions with activity in the last 15 minutes) and "Today" (everything with activity since local midnight).
- **Per-session breakdown**: session id, project, last-activity age, input / output / cache-read / total tokens, cost, and three velocities.
- **Velocity columns**: total throughput (`t/s`, includes cache), generation rate (`out/s`, output tokens only), and cost rate (`$/h`), all over a configurable rolling window.
- **Upgraded status bar**: folder · model · context % (green/yellow/red) · cost · Anthropic 5h rate-limit reset countdown, directly in Claude Code.
- **Graceful degradation**: if `jq` is missing the hook still writes the raw payload and prints a minimal hint instead of silently breaking.
- **Safe by default**: atomic snapshot writes via `mv(1)`, tail-only JSONL reads, and the hook deliberately does not `set -e` so a bad payload can never blank your status bar.

## Requirements

- Python 3.10+
- [`rich`](https://pypi.org/project/rich/) (pinned in `requirements.txt`)
- `jq` on `PATH` — `brew install jq` / `apt install jq` *(POSIX / WSL only; not needed on native Windows)*
- Claude Code installed and having run at least once (so `~/.claude/` exists)

## Installation

```bash
# 1. Install Python deps
python3 -m pip install -r requirements.txt

# 2. Install the statusLine hook into ~/.claude/ and patch settings.json
python3 cc-session-monitor.py --install-hook

# 3. Restart any running Claude Code sessions so the hook picks up

# 4. In a second terminal, launch the monitor
python3 cc-session-monitor.py
```

A convenience wrapper is included:

```bash
./run-monitor.sh                 # uses ./.venv/bin/python if present, else python3
./run-monitor.sh --install-hook  # install the hook
```

### What `--install-hook` does

On **POSIX / macOS / Linux / WSL**:
- Copies `cc-monitor-hook.sh` to `~/.claude/cc-monitor-hook.sh` and `chmod +x`es it.
- Adds (or, after confirmation, replaces) the `statusLine` entry in `~/.claude/settings.json`:
  ```json
  "statusLine": {
    "type": "command",
    "command": "~/.claude/cc-monitor-hook.sh",
    "padding": 0
  }
  ```

On **native Windows** (detected automatically via `os.name == "nt"`):
- Copies `cc-monitor-hook.ps1` to `%USERPROFILE%\.claude\cc-monitor-hook.ps1`.
- Writes a `powershell -File …` command into `settings.json` instead:
  ```json
  "statusLine": {
    "type": "command",
    "command": "powershell -NoProfile -NonInteractive -File C:\\Users\\you\\.claude\\cc-monitor-hook.ps1",
    "padding": 0
  }
  ```

Both paths never touch anything outside `~/.claude/` / `%USERPROFILE%\.claude\`.

## Windows

### Recommended path: WSL (Windows Subsystem for Linux)

Running inside WSL is the simplest Windows option — everything works
out-of-the-box with zero extra steps:

1. Install [WSL](https://learn.microsoft.com/en-us/windows/wsl/install) and a
   Linux distribution (Ubuntu is fine).
2. Install Claude Code in WSL as you would on Linux.
3. Follow the standard installation steps above inside the WSL terminal.

`~/.claude/` inside WSL maps to your Linux home directory; the hook and TUI
run under the WSL bash environment with full `jq` / `awk` support.

### Native Windows (PowerShell)

If you run Claude Code natively on Windows (outside WSL) a PowerShell hook is
provided (`cc-monitor-hook.ps1`) that replaces the bash hook.  No `jq`
dependency — `ConvertFrom-Json` handles JSON parsing.

**Requirements (native Windows only):**

- Windows 10 version 1511 or newer (for ANSI colour support in the terminal).
- PowerShell 5.1 or newer (ships with Windows 10/11).

**Install:**

```powershell
# From the repo root in a PowerShell prompt:
python cc-session-monitor.py --install-hook
```

The installer detects Windows and automatically installs `cc-monitor-hook.ps1`
and writes the `powershell -File …` command into `settings.json`.

**Manual install** (if the auto-install does not work):

```powershell
Copy-Item cc-monitor-hook.ps1 "$env:USERPROFILE\.claude\cc-monitor-hook.ps1"
```

Then add to `%USERPROFILE%\.claude\settings.json`:

```json
"statusLine": {
  "type": "command",
  "command": "powershell -NoProfile -NonInteractive -File C:\\Users\\you\\.claude\\cc-monitor-hook.ps1",
  "padding": 0
}
```

**Smoke-test the hook:**

```powershell
'{"session_id":"test","model":{"display_name":"Opus"},
  "workspace":{"current_dir":"C:\\Users\\you\\myproject"},
  "cost":{"total_cost_usd":0.42},
  "context_window":{"used_percentage":15,
                    "total_input_tokens":1200,
                    "total_output_tokens":800}}' |
  powershell -NoProfile -NonInteractive -File .\cc-monitor-hook.ps1
```

A snapshot should appear under
`%USERPROFILE%\.claude\session-monitor\snapshots\test.json` and a coloured
status line should print to the terminal.

**Run the TUI (native Windows):**

```powershell
python cc-session-monitor.py
```

The Python TUI is cross-platform; no special steps are needed beyond
installing the Python dependencies.

## Usage

```bash
python cc-session-monitor.py [OPTIONS]
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--refresh <sec>` | `1.0` | TUI redraw interval in seconds (float). Smaller = smoother, more CPU. |
| `--velocity-window <sec>` | `30` | Rolling window for the `t/s`, `out/s`, and `$/h` velocity columns. Larger = smoother, smaller = more reactive. |
| `--projects-dir <path>` | `~/.claude/projects` | Where to read JSONL transcripts from. Change only if Claude Code stores data elsewhere. |
| `--snapshot-dir <path>` | `~/.claude/session-monitor/snapshots` | Where the hook writes snapshots. Must match the hook's target. |
| `--install-hook` | — | One-shot: install the hook into `~/.claude/` and exit. No monitor run. |
| `-h`, `--help` | — | Show help and exit. |

### Example

```bash
# Faster redraw + smaller velocity window for near-realtime feedback
python cc-session-monitor.py --refresh 0.5 --velocity-window 10
```

## Reading the TUI

### Columns

| Column | What it shows | Source |
|---|---|---|
| **Session** | First 8 chars of the session UUID, prefixed with a status marker | JSONL filename |
| **Project** | Project folder name (e.g. `-Users-peter-code-foo` → `foo`) | Directory name |
| **Age** | Time since last activity (`45s`, `3m12s`, `1h05m`) | Latest of JSONL / hook |
| **Input** | Cumulative input tokens since session start | Hook (accurate) or JSONL (approx) |
| **Output** | Cumulative output tokens since session start | Hook (accurate) or JSONL (approx) |
| **Cache R** | Cache-read tokens (the cheap, reused kind) | JSONL (reliable) |
| **Total** | `Input + Output + Cache-Read + Cache-Create` | Computed |
| **t/s** | Total token throughput over the velocity window — sums **all** token types (input + output + cache-read + cache-create). Cache-reads usually dominate, so this tracks data flow, not work done. | Computed from JSONL |
| **out/s** | Generation rate: **output tokens per second only**. The "real work" signal — the cost-relevant tokens the model actually produced. See *Accuracy caveat* below. | Computed from JSONL |
| **Cost** | Cumulative USD since session start | Hook |
| **$/h** | Cost rate extrapolated to one hour, over the velocity window | Computed from hook snapshots |

A **TOTAL** footer row sums all sessions currently visible in that panel.

### Session marker

- **`●` green** — hook is installed for this session; `Cost` and context tokens are **accurate**.
- **`○` yellow** — JSONL-only fallback; `Input` / `Output` are **streaming placeholders** (underestimate real billed tokens). Install the hook to fix this.

### Velocity columns — what's a warning, what's just info

Only **`$/h`** is a warning metric (red = burning money). `t/s` and `out/s` are informational — high throughput or fast generation isn't bad, so they're rendered in a single neutral color.

| Column | Color | Meaning |
|---|---|---|
| `t/s` | dim "idle" / cyan | informational — total throughput incl. cache; high values usually mean the cache is shuttling data |
| `out/s` | dim "idle" / **bold cyan** | informational — emphasized because it's the actual generation rate |
| `$/h` | dim `—` / green / yellow / **bold red** | warning metric — green `< $1/h`, yellow `$1–$5/h`, bold red `≥ $5/h` |

#### Accuracy caveat for `out/s` (and `Output`)

`out/s` is computed from `output_tokens` values in Claude Code's JSONL transcripts. Those values are streaming placeholders and tend to **undercount** real billed output (same caveat as the `Output` column on `○`-marked rows). The hook can't supply an accurate series because it only carries a cumulative snapshot — fine for a single number, not enough for a rate. Treat `out/s` as a **good-enough order of magnitude**, not a billing-accurate figure. The `$/h` column remains the authoritative cost-rate signal because it's derived from `total_cost_usd` snapshots written by the hook.

### Reading the velocities together

The interesting signal is the relationship between the three:

| `t/s` | `out/s` | `$/h` | Interpretation |
|---|---|---|---|
| high | low | low | **Cache working well** — lots of context flowing, model not generating much, low cost. Ideal. |
| high | high | high | **Heavy generation, cache not helping enough** — usually Opus reasoning without cache hits. |
| low | low | high | **Short, expensive turns** — small but pricey work (Opus first-token without cache). |
| any | any | low | Quiet or cache-friendly. |

### Status-bar colors (Claude Code itself, not the TUI)

The hook prints a colored one-liner into Claude Code's status bar. The context-percentage segment is color-coded:

- **green** `< 50 %` — plenty of headroom
- **yellow** `50–79 %` — keep an eye on it
- **red** `≥ 80 %` — compaction / context limit approaching

### Panel and accent colors

- **Green panel border** — the "Active" (last 15 min) view.
- **Blue panel border** — the "Today" (since local midnight) view.
- **Magenta** — project name column.
- **Cyan** — table headers, the `t/s` and `out/s` columns (the latter in **bold cyan** for emphasis), and the folder name in the status bar.
- **Dim** — `Cache R` column and separators, deliberately de-emphasized because cache reads are cheap and plentiful.
- The **TOTAL** footer highlights the summed token count on dark-cyan and, if non-zero, the summed cost on green.

The `$/h` thresholds are hard-coded in the cost-velocity block of `build_table` (`cc-session-monitor.py:552`). `t/s` and `out/s` are intentionally single-color (informational, not warnings) — see `_fmt_velocity` / `_fmt_output_velocity` (`cc-session-monitor.py:455`). Tweak any of these if your usage pattern makes the defaults feel off.

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

## History

The monitor persists a JSON snapshot per calendar day to `~/.claude/session-monitor/history/daily/YYYY-MM-DD.json`. Every 60 seconds and on Ctrl-C shutdown the file is refreshed with the state of every session that had activity on that local date.

### Retention

- **Daily files**: today + the previous 2 days (3 total).
- **Older days**: appended as one line to `history/monthly/YYYY-MM.jsonl` and the daily file is deleted. Dedup-safe: re-running retention never duplicates a day.
- **Monthly files**: the 12 most recent are kept, older ones are deleted. Max on disk ≈ 3 daily + 12 monthly files.

### Reconstruction

If the monitor was not running yesterday or the day before, those daily files are reconstructed from Claude Code's JSONL transcripts on the next startup. Reconstructed files have `"reconstructed": true` and `"session_cumulative_cost_usd": null` on every session, because the cumulative cost snapshot in the hook's data cannot be reliably attributed to one specific day after the fact. Token counts are still accurate (modulo the known JSONL placeholder issue).

### CLI

```
--no-log           disable history logging entirely
--history-dir P    alternate location (default: ~/.claude/session-monitor/history)
```

### File format

Daily JSON (and one JSONL line in the monthly file, minus `generated_at`):

```json
{
  "date": "2026-04-23",
  "reconstructed": false,
  "generated_at": 1745403600.0,
  "sessions": {
    "<session_uuid>": {
      "project": "foo",
      "model": "Opus",
      "first_ts": 1745382000.0,
      "last_ts": 1745400000.0,
      "input_tokens": 12345,
      "output_tokens": 6789,
      "cache_read_tokens": 98765,
      "cache_creation_tokens": 4321,
      "session_cumulative_cost_usd": 2.34
    }
  },
  "totals": {
    "sessions": 1,
    "input_tokens": 12345,
    "...": "same fields summed",
    "session_cumulative_cost_usd": 2.34
  }
}
```

`sessions` is keyed by session UUID so external tools can join/diff across days. Token counts are the sum of usage samples whose timestamps fell within that local calendar date — not cumulative session totals. `session_cumulative_cost_usd` is the session lifetime cumulative `total_cost_usd` seen at the most recent tick of that day, not spend attributable to that day alone; summing it across dates will double-count multi-day sessions. For reconstructed files it is `null`.

## Known limitations

- **JSONL input/output tokens undercount.** This is a property of Claude Code's transcripts (see [gille.ai's analysis](https://gille.ai/)), not of this tool. Until the hook has fired at least once for a session, the TUI falls back to JSONL values and marks the row `○` to flag that they're approximate.
- **`out/s` inherits that undercount.** It's computed from the same JSONL `output_tokens` field. The hook only snapshots cumulative state, not a series, so we can't build a hook-backed rate. `out/s` is reliable for "is the model generating, roughly how fast", but not for billing math — use `Cost`/`$/h` for that.
- **Cache tokens come from JSONL only** — the `statusLine` payload doesn't expose them directly. They are reliable there.
- **Sessions outside `~/.claude/projects/`** (rare) are discovered only when the hook fires, since discovery normally walks that directory.

## Files

```
.
├── cc-session-monitor.py   # the TUI
├── cc_history.py           # persistent per-day logger + retention + reconstruction
├── cc-monitor-hook.sh      # the statusLine hook (POSIX / macOS / Linux / WSL)
├── cc-monitor-hook.ps1     # the statusLine hook (native Windows / PowerShell)
├── run-monitor.sh          # convenience wrapper (.venv first, then python3)
├── tests/                  # pytest unit tests for cc_history and install_hook
└── CLAUDE.md               # guidance for Claude Code working in this repo
```

## Development

There is no build step and no lint config. Unit tests (pytest, covering `cc_history.py` and `install_hook`):

```bash
./.venv/bin/pip install -r requirements-dev.txt    # first time only
./.venv/bin/pytest tests/ -v
```

CI runs the same test suite on Python 3.10–3.13 on both `ubuntu-latest` and
`windows-latest` via [`.github/workflows/test.yml`](.github/workflows/test.yml)
on every push and pull request to `main`.

To smoke-test the bash hook in isolation:

```bash
echo '{"session_id":"test","model":{"display_name":"Opus"},
       "workspace":{"current_dir":"/tmp"},
       "cost":{"total_cost_usd":0.42},
       "context_window":{"used_percentage":15,
                         "total_input_tokens":1200,
                         "total_output_tokens":800}}' \
  | ./cc-monitor-hook.sh
```

To smoke-test the PowerShell hook in isolation (see the [Windows](#windows) section above).
