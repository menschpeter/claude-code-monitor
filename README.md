# claude-code-monitor

A live terminal UI for tracking [Claude Code](https://claude.com/claude-code) token usage, estimated API list-price cost, and velocity across all your active sessions — plus a `statusLine` hook that upgrades Claude Code's own status bar with cost estimates and live context-window info.

The scripts inside use a short `cc-` prefix (short for "Claude Code") for internal consistency — the repo name spells it out for discoverability.

Three components work together:

- **`cc-monitor-hook.sh`** — a `statusLine` hook that runs on every message turn. It renders a compact colored status line *and* drops a per-session snapshot JSON to disk.
- **`cc-session-monitor.py`** — a [`rich`](https://github.com/Textualize/rich)-based TUI that reads those snapshots plus Claude Code's JSONL transcripts and shows live per-session tables.
- **`cc_history.py`** — persistent per-day history: one JSON file per calendar day, rolled into monthly JSONL files with bounded retention.

## Why this exists

Claude Code writes streaming transcripts to `~/.claude/projects/<project>/<session>.jsonl`. The `input_tokens` and `output_tokens` fields in those logs are **streaming placeholders** — they undercount API usage and get duplicated across chunks. Cache token fields are reliable, but cost and context-window totals are not present in the JSONL at all.

The [`statusLine` API](https://code.claude.com/docs/en/statusline) receives `cost.total_cost_usd`, a client-side estimate of the current session at standard Anthropic API list prices. It is useful for comparing sessions, but it is not an invoice or an authoritative billing record. In particular, Claude Pro/Max usage included with a subscription is not a per-session API charge. This project bridges that estimate with the JSONL data:

1. The hook persists each turn's raw estimate and builds a monotonic observed-session estimate across counter resets.
2. The TUI merges those snapshots with JSONL-derived tokens and attributes observed estimate deltas to local calendar days.

Rows in the TUI are marked `●` when a hook snapshot is present or `○` when only JSONL is available. A `●` means live snapshot data exists; the estimate can still be `—` when Claude did not provide a valid value or the day cannot be attributed safely.

For billing and organization-wide reporting, use the [Claude Console Usage and Cost API](https://platform.claude.com/docs/en/api/usage-cost-api) or the billing console of the API provider that actually handled the request.

> **Note (Claude Code v2.1.132+):** the hook's `context_window.total_input_tokens` / `total_output_tokens` now report *current context-window occupancy*, not cumulative session totals. The TUI surfaces those in a dedicated **Ctx** column and always derives cumulative Input/Output from the JSONL transcript.

## Features

- **Two views side by side**: "Active" (sessions with activity in the last 15 minutes) and "Today" (everything with activity since local midnight).
- **Per-session breakdown**: session id, project, last-activity age, input / output / cache-read / total tokens, live context-window occupancy (`Ctx`), estimated cost, and three velocities.
- **Effort line**: a footer line naming the reasoning effort level of the most recently active session (see "Effort line" below).
- **Velocity columns**: total throughput (`t/s`, includes cache), generation rate (`out/s`, output tokens only), and estimated list-price rate (`$/h`), all over a configurable rolling window.
- **Upgraded status bar**: folder · model · context % (green/yellow/red) · raw estimate · Anthropic 5h and 7d rate-limit reset countdowns, directly in Claude Code.
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

After updating this repository, run `python3 cc-session-monitor.py --install-hook` again and restart Claude Code. The installed hook is a copy, so it does not update automatically with the checkout.

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

- Windows 10 version 1511 or newer (for ANSI color support in the terminal).
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
`%USERPROFILE%\.claude\session-monitor\snapshots\test.json` and a colored
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
| **Input** | Cumulative input tokens since session start | JSONL (approx — streaming placeholders) |
| **Output** | Cumulative output tokens since session start | JSONL (approx — streaming placeholders) |
| **Cache R** | Cache-read tokens (the cheap, reused kind) | JSONL (reliable) |
| **Total** | `Input + Output + Cache-Read + Cache-Create` | Computed from JSONL |
| **Ctx** | Live context-window occupancy as `used/size` (current load, not cumulative) — drops after `/compact`. Colored like the status-bar gauge. | Hook |
| **t/s** | Total token throughput over the velocity window — sums **all** token types (input + output + cache-read + cache-create). Cache-reads usually dominate, so this tracks data flow, not work done. | Computed from JSONL |
| **out/s** | Generation rate: **output tokens per second only**. The "real work" signal — tokens the model actually produced. See *Accuracy caveat* below. | Computed from JSONL |
| **Est. session $** | Reset-safe observed session estimate in the Active panel | Hook snapshots |
| **Est. today $** | Estimate deltas attributable to the local calendar day in the Today panel | Hook snapshots + daily baseline |
| **$/h** | Estimated API list-price rate extrapolated to one hour, over the velocity window | Computed from normalized hook snapshots |

A **TOTAL** footer row sums all sessions currently visible in that panel. If any visible session's estimate is unknown, the dollar total is `—` rather than a misleading partial sum.

### Effort line

At the top of the footer at the bottom of the screen — ahead of the legend lines — the monitor shows the reasoning effort level of the most recently active session:

```
effort: xhigh  (2e2615e8, most recent)
```

It goes first (rather than below the legend) so it survives rich's line-wrapping at narrower terminal widths instead of being the row that gets clipped.

It is scoped to one session — the one with the newest activity, named by its session-ID prefix — because effort is a per-session setting and a single unlabelled value spanning several sessions would be ambiguous. The line is omitted entirely when that session has not produced an assistant turn carrying an effort level yet — either because its model does not support one or because it has not produced an assistant turn yet.

The value is read from the JSONL transcript, so it reflects the effort **used for the last request** rather than the level currently configured. Change the level with no follow-up request and the line keeps showing the previous value until the next assistant turn. This also means it works for `○`-marked (JSONL-only) sessions, not just hook-backed ones.

### Session marker

- **`●` green** — a hook snapshot exists for this session. The live `Ctx` gauge is available; an estimated dollar value may still be `—`.
- **`○` yellow** — JSONL-only fallback; no estimate or `Ctx` yet. `Input` / `Output` are JSONL-derived for **both** markers (streaming placeholders that slightly undercount API usage). Install the hook to add estimate + context tracking.

### Velocity columns — what's a warning, what's just info

Only **`$/h`** uses warning colors. `t/s` and `out/s` are informational — high throughput or fast generation isn't bad, so they're rendered in a single neutral color. The thresholds classify the list-price estimate, not actual subscription spend.

| Column | Color | Meaning |
|---|---|---|
| `t/s` | dim "idle" / cyan | informational — total throughput incl. cache; high values usually mean the cache is shuttling data |
| `out/s` | dim "idle" / **bold cyan** | informational — emphasized because it's the actual generation rate |
| `$/h` | dim `—` / green / yellow / **bold red** | warning metric — green `< $1/h`, yellow `$1–$5/h`, bold red `≥ $5/h` |

#### Accuracy caveat for `out/s` (and `Output`)

`out/s` is computed from `output_tokens` values in Claude Code's JSONL transcripts. Those values are streaming placeholders and tend to **undercount** API output. The hook cannot supply an output-token series because it only carries cumulative state. Treat `out/s` as a **good-enough order of magnitude**, not a billing figure. `$/h` is the corresponding rate of the hook's client-side list-price estimate; it is more internally consistent for comparisons, but it is still not an authoritative charge.

### Reading the velocities together

The interesting signal is the relationship between the three:

| `t/s` | `out/s` | `$/h` | Interpretation |
|---|---|---|---|
| high | low | low | **Cache working well** — lots of context flowing, model not generating much, low cost. Ideal. |
| high | high | high | **Heavy generation, cache not helping enough** — usually Opus reasoning without cache hits. |
| low | low | high | **Short, expensive turns** — small but pricey work (Opus first-token without cache). |
| any | any | low | Quiet or cache-friendly. |

### Status-bar colors (Claude Code itself, not the TUI)

The hook prints a colored one-liner into Claude Code's status bar:
`folder · model · ctx % · est $… · 5h limit · 7d limit`. `est —` means the current payload had no valid numeric estimate. The context-percentage segment is color-coded:

- **green** `< 50 %` — plenty of headroom
- **yellow** `50–79 %` — keep an eye on it
- **red** `≥ 80 %` — compaction / context limit approaching

The `5h` and `7d` rate-limit segments (e.g. `5h 23% (2h0m) │ 7d 41% (2d7h)`) appear only for Claude.ai Pro/Max subscribers after the first API response; the `7d` window requires Claude Code v2.1.132+.

### Panel and accent colors

- **Green panel border** — the "Active" (last 15 min) view.
- **Blue panel border** — the "Today" (since local midnight) view.
- **Magenta** — project name column.
- **Cyan** — table headers, the `t/s` and `out/s` columns (the latter in **bold cyan** for emphasis), and the folder name in the status bar.
- **Dim** — `Cache R` column and separators, deliberately de-emphasized because cache reads are cheap and plentiful.
- The **TOTAL** footer highlights the summed token count on dark-cyan and the complete known estimate on green, including a known `$0.00`. It shows `—` if any visible estimate is unknown.

The `$/h` thresholds are hard-coded in the cost-velocity block of `build_table`. `t/s` and `out/s` are intentionally single-color (informational, not warnings) — see `_fmt_velocity` / `_fmt_output_velocity`. Tweak any of these if your usage pattern makes the defaults feel off.

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
- **Estimate normalization**: each hook snapshot retains the previous valid raw and observed estimate. A lower raw counter increments the reset count and starts a new epoch without making the observed session estimate decrease.
- **Daily attribution**: the monitor adds normalized estimate deltas to the local date of each snapshot. Today's saved baseline is restored on startup; unsafe or unavailable values remain `null`/`—`.
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

If the monitor was not running yesterday or the day before, those daily files are reconstructed from Claude Code's JSONL transcripts on the next startup. Reconstructed files have `"reconstructed": true`, `"estimated_cost_usd": null`, and `"session_cumulative_cost_usd": null` on every session because no post-hoc JSONL calculation can reproduce the hook estimate or attribute it safely to one day. Token counts retain the known JSONL placeholder caveat.

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
      "estimated_cost_usd": 4.25,
      "last_observed_total_cost_usd": 12.75,
      "last_raw_cost_usd": 1.50,
      "cost_counter_resets": 2,
      "last_cost_ts": 1776945600.0,
      "session_cumulative_cost_usd": 1.50
    }
  },
  "totals": {
    "sessions": 1,
    "input_tokens": 12345,
    "...": "token fields summed",
    "estimated_cost_usd": 4.25,
    "session_cumulative_cost_usd": 1.50
  }
}
```

`sessions` is keyed by session UUID so external tools can join/diff across days. Token counts are the sum of usage samples whose timestamps fell within that local calendar date — not cumulative session totals.

- `estimated_cost_usd` is the sum of observed estimate deltas attributed to that local date. It is `null` when there is no safe baseline, when the normalized total decreases unexpectedly, or when the file was reconstructed. The daily total is also `null` if any session is unknown.
- `last_observed_total_cost_usd`, `last_raw_cost_usd`, `cost_counter_resets`, and `last_cost_ts` let the next monitor process resume the attribution baseline without double-counting.
- `session_cumulative_cost_usd` is a deprecated compatibility field containing the latest raw session counter. It is not attributable daily spend and must not be summed across dates. Older history files without the new fields remain readable.

## Known limitations

- **Dollar values are estimates, not bills.** Claude Code computes `total_cost_usd` client-side at standard API list prices. Pro/Max included usage is not a per-session charge, and third-party providers can price requests differently. Use the Claude Console Usage and Cost API or the provider's billing console for authoritative amounts.
- **Counter continuity is observational.** The hook makes resets monotonic by retaining the previous atomic snapshot. Deleting snapshots or first installing the updated hook starts a new observation baseline; unsafe Today/history attribution stays `null`/`—` instead of being guessed.
- **JSONL input/output tokens undercount.** This is a property of Claude Code's transcripts (see [gille.ai's analysis](https://gille.ai/)), not of this tool. The `Input` / `Output` / `Total` columns are always JSONL-derived (since CC v2.1.132 the hook no longer exposes cumulative token totals — only current context-window occupancy, shown separately in `Ctx`), so they slightly undercount API usage regardless of the row marker. The `●`/`○` marker reflects only whether a hook snapshot and the `Ctx` gauge are available.
- **`out/s` inherits that undercount.** It is computed from the same JSONL `output_tokens` field. The hook only snapshots cumulative state, not an output-token series, so `out/s` is useful for "is the model generating, roughly how fast", not billing math. `Est. … $` and `$/h` are list-price estimates, not substitutes for provider billing data.
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

There is no build step and no lint config. Unit tests cover history, both hook contracts, installation, context handling, cost attribution/rendering, and the effort footer:

```bash
./.venv/bin/pip install -r requirements-dev.txt    # first time only
./.venv/bin/pytest tests/ -v
```

CI runs the same test suite on Python 3.10–3.14 on both `ubuntu-latest` and
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
