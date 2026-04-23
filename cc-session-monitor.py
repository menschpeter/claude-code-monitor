#!/usr/bin/env python3
"""
cc-session-monitor — live Terminal UI for tracking Claude Code token usage
per active session, with token/s velocity.

Reads Claude Code's JSONL transcripts under ~/.claude/projects/<project>/*.jsonl
and renders two side-by-side panels:

  • ACTIVE (last 5 min) — sessions with activity in the last 5 minutes
  • BILLING WINDOW (5h)  — all sessions in the current 5h billing block

For each session the monitor shows:
  • SessionID (shortened)   — the .jsonl UUID
  • Project                 — derived from the directory name
  • Last activity (age)
  • input / output / cache_read / cache_creation tokens (cumulative)
  • Total tokens
  • Velocity: tokens/second over a rolling window (default 30s)

Known caveat (documented issue in Claude Code JSONL logs):
  input_tokens and output_tokens in the JSONL are streaming placeholders
  and undercount vs. the real billed amounts (see gille.ai analysis).
  Cache fields are accurate. The tool deduplicates by requestId and uses
  per-requestId MAX to mitigate streaming duplicates, but absolute
  input/output numbers remain approximate. Velocity and relative trends
  between sessions are still meaningful.

Usage:
    python cc-session-monitor.py
    python cc-session-monitor.py --refresh 1.0 --velocity-window 30

Requires: rich  (pip install rich)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.text import Text
    from rich.align import Align
except ImportError:
    sys.stderr.write(
        "This tool needs 'rich'. Install it with:\n    pip install rich\n"
    )
    sys.exit(1)


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
SNAPSHOT_DIR = Path.home() / ".claude" / "session-monitor" / "snapshots"
ACTIVE_WINDOW_SECONDS = 5 * 60          # "active" = activity in last 5 min
BILLING_WINDOW_SECONDS = 5 * 60 * 60    # Anthropic's rolling 5h window
VELOCITY_WINDOW_SECONDS = 30            # default rolling velocity window


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class UsageSample:
    """A single deduplicated usage observation tied to a requestId."""
    ts: float                       # unix seconds
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation: int = 0
    cache_read: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation
            + self.cache_read
        )


@dataclass
class SessionState:
    session_id: str                 # .jsonl filename stem (uuid)
    project: str                    # derived from directory name
    jsonl_path: Path
    file_size: int = 0              # last seen size, for tail-only reads

    # dedup: requestId -> best usage sample (MAX strategy for output_tokens)
    samples: dict[str, UsageSample] = field(default_factory=dict)

    # rolling velocity: (ts, total_tokens_at_ts) pairs
    velocity_points: deque = field(default_factory=lambda: deque(maxlen=2000))

    first_ts: float | None = None
    last_ts: float | None = None

    # ----- hook-derived fields (None until the hook fires at least once) -----
    # These are ACCURATE, unlike JSONL input/output placeholders.
    hook_ts: float | None = None            # when the last snapshot was written
    hook_cost_usd: float | None = None      # cost.total_cost_usd
    hook_ctx_pct: float | None = None       # context_window.used_percentage
    hook_ctx_input: int | None = None       # context_window.total_input_tokens
    hook_ctx_output: int | None = None      # context_window.total_output_tokens
    hook_model: str | None = None
    hook_cwd: str | None = None
    hook_rl5_pct: float | None = None       # 5h rate-limit %
    hook_rl5_reset: int | None = None       # epoch seconds

    # rolling history of (ts, cost_usd) for $/h velocity
    cost_points: deque = field(default_factory=lambda: deque(maxlen=500))

    # ----- aggregates -----

    def totals(self) -> UsageSample:
        agg = UsageSample(ts=self.last_ts or 0.0)
        for s in self.samples.values():
            agg.input_tokens += s.input_tokens
            agg.output_tokens += s.output_tokens
            agg.cache_creation += s.cache_creation
            agg.cache_read += s.cache_read
        return agg

    def totals_since(self, cutoff_ts: float) -> UsageSample:
        agg = UsageSample(ts=self.last_ts or 0.0)
        for s in self.samples.values():
            if s.ts >= cutoff_ts:
                agg.input_tokens += s.input_tokens
                agg.output_tokens += s.output_tokens
                agg.cache_creation += s.cache_creation
                agg.cache_read += s.cache_read
        return agg

    def velocity(self, window_seconds: int, now: float) -> float:
        """Tokens per second over the last `window_seconds`."""
        if len(self.velocity_points) < 2:
            return 0.0
        cutoff = now - window_seconds
        # find first point inside window
        pts = [p for p in self.velocity_points if p[0] >= cutoff]
        if len(pts) < 2:
            # fall back to the last two points if the window is too narrow
            pts = list(self.velocity_points)[-2:]
        (t0, v0), (t1, v1) = pts[0], pts[-1]
        dt = t1 - t0
        if dt <= 0:
            return 0.0
        return max(0.0, (v1 - v0) / dt)

    def cost_velocity(self, window_seconds: int, now: float) -> float:
        """USD per hour over the last `window_seconds`. 0 if unknown."""
        if len(self.cost_points) < 2:
            return 0.0
        cutoff = now - window_seconds
        pts = [p for p in self.cost_points if p[0] >= cutoff]
        if len(pts) < 2:
            pts = list(self.cost_points)[-2:]
        (t0, c0), (t1, c1) = pts[0], pts[-1]
        dt = t1 - t0
        if dt <= 0:
            return 0.0
        return max(0.0, (c1 - c0) / dt * 3600.0)

    def effective_last_ts(self) -> float | None:
        """Latest signal of activity — from JSONL or from the hook snapshot."""
        candidates = [t for t in (self.last_ts, self.hook_ts) if t is not None]
        return max(candidates) if candidates else None


# ---------------------------------------------------------------------------
# JSONL parsing — tolerate Claude Code's placeholder / duplicate streaming
# ---------------------------------------------------------------------------

def _parse_ts(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        # JSONL timestamps are ISO-8601 with trailing Z
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def _extract_usage(entry: dict) -> tuple[str | None, UsageSample | None]:
    """Pull (requestId, UsageSample) out of one JSONL line, or (None, None)."""
    if entry.get("type") != "assistant":
        return None, None

    msg = entry.get("message") or {}
    usage = msg.get("usage") or {}
    if not usage:
        return None, None

    req_id = (
        entry.get("requestId")
        or msg.get("id")
        or msg.get("request_id")
    )
    ts = _parse_ts(entry.get("timestamp"))
    if ts is None:
        return None, None

    return req_id, UsageSample(
        ts=ts,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_creation=int(usage.get("cache_creation_input_tokens") or 0),
        cache_read=int(usage.get("cache_read_input_tokens") or 0),
    )


def _merge_sample(existing: UsageSample | None, new: UsageSample) -> UsageSample:
    """MAX merge: streaming duplicates hold placeholder values, so we keep
    the largest observation per field and the latest timestamp."""
    if existing is None:
        return new
    return UsageSample(
        ts=max(existing.ts, new.ts),
        input_tokens=max(existing.input_tokens, new.input_tokens),
        output_tokens=max(existing.output_tokens, new.output_tokens),
        cache_creation=max(existing.cache_creation, new.cache_creation),
        cache_read=max(existing.cache_read, new.cache_read),
    )


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class Monitor:
    def __init__(
        self,
        root: Path = CLAUDE_PROJECTS_DIR,
        snapshot_dir: Path = SNAPSHOT_DIR,
        velocity_window: int = VELOCITY_WINDOW_SECONDS,
    ) -> None:
        self.root = root
        self.snapshot_dir = snapshot_dir
        self.velocity_window = velocity_window
        self.sessions: dict[str, SessionState] = {}  # key = session_id
        self._snapshot_mtimes: dict[str, float] = {}  # session_id -> last mtime seen

    # ---- discovery ----

    def _iter_session_files(self) -> Iterable[tuple[str, Path]]:
        if not self.root.exists():
            return
        for project_dir in self.root.iterdir():
            if not project_dir.is_dir():
                continue
            project_name = _humanize_project(project_dir.name)
            for jsonl in project_dir.glob("*.jsonl"):
                yield project_name, jsonl

    def refresh(self) -> None:
        """Re-scan directory, tail any grown files."""
        for project, jsonl in self._iter_session_files():
            session_id = jsonl.stem
            try:
                size = jsonl.stat().st_size
            except FileNotFoundError:
                continue

            state = self.sessions.get(session_id)
            if state is None:
                state = SessionState(
                    session_id=session_id,
                    project=project,
                    jsonl_path=jsonl,
                )
                self.sessions[session_id] = state

            if size == state.file_size:
                continue  # nothing new

            # Read from last seen offset forward. If file shrank (log rotate
            # or edit), start from scratch.
            start = state.file_size if size > state.file_size else 0
            try:
                with jsonl.open("rb") as f:
                    f.seek(start)
                    data = f.read()
            except OSError:
                continue

            if start == 0:
                # full reset of the in-memory view
                state.samples.clear()
                state.velocity_points.clear()
                state.first_ts = None
                state.last_ts = None

            state.file_size = size

            # split on newlines; last chunk may be partial if we caught it
            # mid-write, so skip trailing non-terminated line
            lines = data.split(b"\n")
            if data and not data.endswith(b"\n"):
                lines = lines[:-1]

            for raw in lines:
                if not raw.strip():
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                req_id, sample = _extract_usage(entry)
                if sample is None:
                    continue

                # Dedup key: prefer requestId; fall back to message id or ts
                key = req_id or f"ts:{sample.ts}"
                state.samples[key] = _merge_sample(
                    state.samples.get(key), sample
                )

                if state.first_ts is None or sample.ts < state.first_ts:
                    state.first_ts = sample.ts
                if state.last_ts is None or sample.ts > state.last_ts:
                    state.last_ts = sample.ts

            # Rebuild velocity series: (ts, running_total) sorted by ts.
            # Cheap enough; sessions rarely exceed a few thousand samples.
            ordered = sorted(state.samples.values(), key=lambda s: s.ts)
            running = 0
            state.velocity_points.clear()
            for s in ordered:
                running += s.total
                state.velocity_points.append((s.ts, running))

        # ----- Second pass: read any snapshot files dropped by the hook -----
        self._refresh_snapshots()

    def _refresh_snapshots(self) -> None:
        """Load hook-written snapshots. Files are tiny; we reread them only
        when mtime changes."""
        if not self.snapshot_dir.exists():
            return
        for snap in self.snapshot_dir.glob("*.json"):
            # Skip hidden tmp files and the raw fallback
            if snap.name.startswith((".", "_")):
                continue
            try:
                mtime = snap.stat().st_mtime
            except FileNotFoundError:
                continue
            prev_mtime = self._snapshot_mtimes.get(snap.stem)
            if prev_mtime is not None and mtime <= prev_mtime:
                continue
            try:
                with snap.open() as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            self._snapshot_mtimes[snap.stem] = mtime
            self._apply_snapshot(data)

    def _apply_snapshot(self, data: dict) -> None:
        session_id = data.get("session_id") or ""
        if not session_id:
            return
        state = self.sessions.get(session_id)
        if state is None:
            # Snapshot arrived before JSONL was discovered — e.g. a session
            # whose transcript is outside the projects dir. Stub it in so
            # it's still tracked. project name is derived from cwd.
            cwd = data.get("cwd") or ""
            project = Path(cwd).name if cwd else "?"
            jsonl_path = Path(data.get("transcript_path") or "")
            state = SessionState(
                session_id=session_id,
                project=project,
                jsonl_path=jsonl_path,
            )
            self.sessions[session_id] = state

        snap_ts = float(data.get("snapshot_ts") or 0.0)
        if snap_ts <= 0:
            return

        ctx = data.get("context_window") or {}
        cost = data.get("cost") or {}
        rl5 = (data.get("rate_limits") or {}).get("five_hour") or {}

        state.hook_ts = snap_ts
        state.hook_cost_usd = float(cost.get("total_cost_usd") or 0.0)
        state.hook_ctx_pct = float(ctx.get("used_percentage") or 0.0)
        state.hook_ctx_input = int(ctx.get("total_input_tokens") or 0)
        state.hook_ctx_output = int(ctx.get("total_output_tokens") or 0)
        state.hook_model = data.get("model") or state.hook_model
        state.hook_cwd = data.get("cwd") or state.hook_cwd
        state.hook_rl5_pct = float(rl5.get("used_percentage") or 0.0) or None
        reset = rl5.get("resets_at")
        state.hook_rl5_reset = int(reset) if reset else None

        # Append to cost history (for $/h velocity). Dedup by ts to avoid
        # noise if the hook re-fires with the same snapshot.
        if (not state.cost_points
                or state.cost_points[-1][0] < snap_ts):
            state.cost_points.append((snap_ts, state.hook_cost_usd))

    # ---- views ----

    def active_sessions(self, now: float) -> list[SessionState]:
        cutoff = now - ACTIVE_WINDOW_SECONDS
        return sorted(
            (
                s for s in self.sessions.values()
                if (ts := s.effective_last_ts()) is not None and ts >= cutoff
            ),
            key=lambda s: s.effective_last_ts() or 0,
            reverse=True,
        )

    def billing_window_sessions(self, now: float) -> list[SessionState]:
        cutoff = now - BILLING_WINDOW_SECONDS
        return sorted(
            (
                s for s in self.sessions.values()
                if (ts := s.effective_last_ts()) is not None and ts >= cutoff
            ),
            key=lambda s: s.effective_last_ts() or 0,
            reverse=True,
        )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _humanize_project(encoded: str) -> str:
    """Claude Code encodes project paths as directory names like
    '-Users-peter-code-myproj' — turn that into 'myproj'."""
    if encoded.startswith("-"):
        encoded = encoded[1:]
    parts = encoded.split("-")
    return parts[-1] if parts else encoded


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds/60)}m{int(seconds)%60:02d}s"
    h = int(seconds / 3600)
    m = int((seconds % 3600) / 60)
    return f"{h}h{m:02d}m"


def _fmt_velocity(tps: float) -> Text:
    if tps >= 1000:
        t = Text(f"{tps/1000:.2f}K t/s", style="bold red")
    elif tps >= 100:
        t = Text(f"{tps:.0f} t/s", style="bold yellow")
    elif tps > 0:
        t = Text(f"{tps:.1f} t/s", style="green")
    else:
        t = Text("idle", style="dim")
    return t


def build_table(
    title: str,
    sessions: list[SessionState],
    now: float,
    velocity_window: int,
    scope_cutoff: float | None = None,
) -> Table:
    """
    scope_cutoff: if given, only tokens accumulated at/after this ts are
    summed (used for the 5h billing view to reflect only what counts in
    the current window).
    """
    table = Table(
        title=title,
        expand=True,
        header_style="bold cyan",
        border_style="dim",
        pad_edge=False,
    )
    table.add_column("Session", style="bold", no_wrap=True)
    table.add_column("Project", style="magenta", no_wrap=True)
    table.add_column("Age", justify="right", no_wrap=True)
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Cache R", justify="right", style="dim")
    table.add_column("Total", justify="right", style="bold")
    table.add_column("t/s", justify="right")
    table.add_column("Cost", justify="right", style="green")
    table.add_column("$/h", justify="right")

    if not sessions:
        table.add_row(
            Text("—", style="dim"),
            Text("no sessions in window (hook not installed? start a "
                 "Claude Code session, or run `--install-hook`)",
                 style="dim italic"),
            "", "", "", "", "", "", "", "",
        )
        return table

    grand_total_tokens = 0
    grand_total_cost = 0.0
    for s in sessions:
        totals = s.totals_since(scope_cutoff) if scope_cutoff else s.totals()
        age = now - (s.effective_last_ts() or now)
        vel = s.velocity(velocity_window, now)

        # Prefer hook values for Input/Output (accurate cumulative totals).
        # Fall back to JSONL-derived values when no hook data is available yet.
        input_tok = s.hook_ctx_input if s.hook_ctx_input is not None else totals.input_tokens
        output_tok = s.hook_ctx_output if s.hook_ctx_output is not None else totals.output_tokens

        # Total: cache tokens come from JSONL (reliable there), input/output
        # from whatever source we picked above.
        row_total = input_tok + output_tok + totals.cache_read + totals.cache_creation
        grand_total_tokens += row_total

        cost_txt = (
            Text(f"${s.hook_cost_usd:.2f}" if s.hook_cost_usd >= 1
                 else f"${s.hook_cost_usd:.3f}", style="green")
            if s.hook_cost_usd is not None else Text("—", style="dim")
        )
        if s.hook_cost_usd is not None:
            grand_total_cost += s.hook_cost_usd

        cvel = s.cost_velocity(velocity_window, now)
        cvel_txt = (
            Text(f"${cvel:.2f}/h",
                 style=("bold red" if cvel >= 5 else
                        "yellow" if cvel >= 1 else "green"))
            if cvel > 0 else Text("—", style="dim")
        )

        # Mark rows whose numbers are hook-backed (accurate) vs JSONL-only.
        marker = "●" if s.hook_ts is not None else "○"
        marker_color = "green" if s.hook_ts is not None else "yellow"

        table.add_row(
            Text(f"{marker} ", style=marker_color).append(
                s.session_id[:8], style="bold"),
            s.project,
            _fmt_age(age),
            _fmt_tokens(input_tok),
            _fmt_tokens(output_tok),
            _fmt_tokens(totals.cache_read),
            _fmt_tokens(row_total),
            _fmt_velocity(vel),
            cost_txt,
            cvel_txt,
        )

    # Summary footer
    table.add_section()
    cost_cell = (Text(f"${grand_total_cost:.2f}",
                      style="bold white on green")
                 if grand_total_cost > 0
                 else Text("—", style="dim"))
    table.add_row(
        Text("TOTAL", style="bold"),
        Text(f"{len(sessions)} session(s)", style="dim"),
        "", "", "", "",
        Text(_fmt_tokens(grand_total_tokens), style="bold white on dark_cyan"),
        "",
        cost_cell,
        "",
    )
    return table


def build_layout(
    monitor: Monitor,
    velocity_window: int,
) -> Layout:
    now = time.time()
    billing_cutoff = now - BILLING_WINDOW_SECONDS

    active = monitor.active_sessions(now)
    window = monitor.billing_window_sessions(now)

    active_tbl = build_table(
        f"🔥 Active sessions (last {ACTIVE_WINDOW_SECONDS // 60} min)",
        active,
        now,
        velocity_window,
    )
    billing_tbl = build_table(
        "📊 Current 5h billing window",
        window,
        now,
        velocity_window,
        scope_cutoff=billing_cutoff,
    )

    header = Text.assemble(
        ("cc-session-monitor", "bold white"),
        ("   │   ", "dim"),
        (f"refresh {time.strftime('%H:%M:%S')}", "cyan"),
        ("   │   ", "dim"),
        (f"velocity window: {velocity_window}s", "cyan"),
        ("   │   ", "dim"),
        ("Ctrl-C to quit", "dim"),
    )

    footer = Text(
        "● hook installed (accurate cost + tokens)   "
        "○ JSONL-only (approximate: streaming placeholders)   "
        "install hook: --install-hook",
        style="dim italic",
    )

    layout = Layout()
    layout.split_column(
        Layout(Align.center(header), name="header", size=1),
        Layout(name="active"),
        Layout(name="billing"),
        Layout(Align.center(footer), name="footer", size=2),
    )
    layout["active"].update(Panel(active_tbl, border_style="green"))
    layout["billing"].update(Panel(billing_tbl, border_style="blue"))
    return layout


# ---------------------------------------------------------------------------
# Hook installer
# ---------------------------------------------------------------------------

def install_hook() -> int:
    """Copy the hook script next to ~/.claude/ and patch settings.json."""
    # The hook ships beside this .py file under the name below.
    here = Path(__file__).resolve().parent
    src = here / "cc-monitor-hook.sh"
    if not src.exists():
        sys.stderr.write(
            f"Hook script not found at {src}.\n"
            "It should sit next to cc-session-monitor.py. "
            "Re-download the bundle.\n"
        )
        return 2

    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(exist_ok=True)
    dest = claude_dir / "cc-monitor-hook.sh"

    dest.write_text(src.read_text())
    dest.chmod(0o755)

    settings_path = claude_dir / "settings.json"
    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            sys.stderr.write(
                f"⚠  {settings_path} exists but is not valid JSON. "
                "Refusing to overwrite.\n"
                "Add this entry manually:\n\n"
                '  "statusLine": {\n'
                '    "type": "command",\n'
                f'    "command": "{dest}",\n'
                '    "padding": 0\n'
                '  }\n'
            )
            return 3

    existing = settings.get("statusLine")
    if existing and existing.get("command") != str(dest):
        print(
            f"⚠  settings.json already has a statusLine: "
            f"{existing.get('command')!r}"
        )
        ans = input("Replace it? [y/N] ").strip().lower()
        if ans != "y":
            print("Aborted. No changes made.")
            return 0

    settings["statusLine"] = {
        "type": "command",
        "command": str(dest),
        "padding": 0,
    }
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")

    print(f"✓ Installed hook to  {dest}")
    print(f"✓ Updated            {settings_path}")
    print(f"✓ Snapshots will go to  {SNAPSHOT_DIR}")
    print()
    print("Restart any running Claude Code sessions to pick up the hook.")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument(
        "--refresh", type=float, default=1.0,
        help="refresh interval in seconds (default: 1.0)",
    )
    ap.add_argument(
        "--velocity-window", type=int, default=VELOCITY_WINDOW_SECONDS,
        help=f"rolling velocity window in seconds "
             f"(default: {VELOCITY_WINDOW_SECONDS})",
    )
    ap.add_argument(
        "--projects-dir", type=Path, default=CLAUDE_PROJECTS_DIR,
        help=f"path to Claude Code projects dir "
             f"(default: {CLAUDE_PROJECTS_DIR})",
    )
    ap.add_argument(
        "--snapshot-dir", type=Path, default=SNAPSHOT_DIR,
        help=f"path where the hook writes session snapshots "
             f"(default: {SNAPSHOT_DIR})",
    )
    ap.add_argument(
        "--install-hook", action="store_true",
        help="install the statusLine hook into ~/.claude/ and exit",
    )
    args = ap.parse_args()

    if args.install_hook:
        return install_hook()

    if not args.projects_dir.exists():
        sys.stderr.write(
            f"Claude projects dir not found: {args.projects_dir}\n"
            "Has Claude Code ever been run on this machine?\n"
        )
        return 2

    monitor = Monitor(
        root=args.projects_dir,
        snapshot_dir=args.snapshot_dir,
        velocity_window=args.velocity_window,
    )
    console = Console()

    try:
        with Live(
            build_layout(monitor, args.velocity_window),
            console=console,
            refresh_per_second=max(1.0, 1.0 / args.refresh),
            screen=True,
        ) as live:
            while True:
                monitor.refresh()
                live.update(build_layout(monitor, args.velocity_window))
                time.sleep(args.refresh)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
